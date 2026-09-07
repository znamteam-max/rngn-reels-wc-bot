from __future__ import annotations

import csv
import io
import re
from datetime import date, datetime, timezone
from difflib import SequenceMatcher
from typing import Any

import requests

from bot import db, multiplatform_metrics
from bot.links import normalize_instagram, normalize_tiktok, normalize_vk, normalize_youtube


VIDEOS_V2_SUFFIX = "/videos-v2.tsv"
RECENT_DAYS = 60
AUTO_MAX_DAYS = 3
SUGGEST_MAX_DAYS = 7
AMBIGUITY_MARGIN = 0.08
PLATFORM_FIELDS = {
    "instagram": ("Instagram URL", "instagram_url", "instagram_id"),
    "youtube": ("YouTube URL", "youtube_url", "youtube_id"),
    "tiktok": ("TikTok URL", "tiktok_url", "tiktok_id"),
    "vk": ("VK URL", "vk_url", None),
}
STOP_WORDS = {
    "подпишись", "подписывайся", "смотрим", "смотрите", "здесь", "живет", "живёт",
    "видео", "video", "shorts", "short", "reels", "reel", "credit", "copyright",
    "спасибо", "лайк", "лайки", "поставьте", "ставьте", "подробнее", "ссылка",
    "нашем", "наш", "канал", "канале", "vk", "instagram", "youtube", "tiktok",
    "nba", "нба", "basketball", "баскетбол", "взял", "мяч", "клипс",
}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _videos_v2_url() -> str:
    bridge = multiplatform_metrics.CONTENT_CORE_BRIDGE_URL
    prefix, marker, rest = bridge.partition("/mirror/")
    if not marker or "/" not in rest:
        raise RuntimeError("Content Core bridge URL has unexpected format")
    token = rest.split("/", 1)[0]
    return f"{prefix.rstrip('/')}/mirror/{token}{VIDEOS_V2_SUFFIX}"


def _fetch_rows() -> list[dict[str, str]]:
    response = requests.get(_videos_v2_url(), timeout=45)
    response.raise_for_status()
    return list(csv.DictReader(io.StringIO(response.text), delimiter="\t"))


def _family(value: Any) -> str:
    text = _text(value).lower().replace("ё", "е")
    text = re.sub(r"[^a-zа-я0-9]+", " ", text)
    if "взял мяч" in text or "vzyal myach" in text or "vzyal_myach" in text:
        return "vzyal_myach"
    return text.strip()


def _tokens(value: Any) -> list[str]:
    text = _text(value).lower().replace("ё", "е")
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"[@#][\w.-]+", " ", text, flags=re.UNICODE)
    text = re.sub(r"[^\w]+", " ", text, flags=re.UNICODE)
    return [token for token in text.split() if len(token) >= 2 and token not in STOP_WORDS][:32]


def _norm(value: Any) -> str:
    return " ".join(_tokens(value))


def _similarity(left: Any, right: Any) -> tuple[float, int, float, float]:
    left_tokens = set(_tokens(left))
    right_tokens = set(_tokens(right))
    if not left_tokens or not right_tokens:
        return 0.0, 0, 0.0, 0.0
    common = len(left_tokens & right_tokens)
    union = len(left_tokens | right_tokens)
    jaccard = common / union if union else 0.0
    containment = common / min(len(left_tokens), len(right_tokens))
    sequence = SequenceMatcher(None, _norm(left), _norm(right)).ratio()
    score = max(jaccard, containment * 0.92, sequence * 0.90)
    return score, common, jaccard, containment


def _row_date(row: dict[str, str]) -> date | None:
    raw = _text(row.get("Дата"))
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%d.%m.%Y").date()
    except ValueError:
        try:
            return datetime.fromisoformat(raw[:10]).date()
        except ValueError:
            return None


def _video_date(video: dict[str, Any]) -> date | None:
    value = video.get("publish_date")
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raw = _text(value)
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw[:10]).date()
    except ValueError:
        return None


def _days(a: date, b: date) -> int:
    return abs((a - b).days)


def _int_or_none(value: Any) -> int | None:
    raw = _text(value).replace(" ", "").replace("\u00a0", "")
    if not raw:
        return None
    try:
        return int(float(raw.replace(",", ".")))
    except ValueError:
        return None


def _duration_compatible(a: int | None, b: int | None) -> bool:
    if not a or not b:
        return True
    return abs(a - b) <= max(2500, int(min(a, b) * 0.08))


def _platform_id(platform: str, url: str) -> str:
    if not url:
        return ""
    try:
        if platform == "instagram":
            return normalize_instagram(url).external_id or ""
        if platform == "youtube":
            return normalize_youtube(url).external_id or ""
        if platform == "tiktok":
            return normalize_tiktok(url).external_id or ""
        if platform == "vk":
            info = normalize_vk(url)
            return multiplatform_metrics._normalize_vk_id(info.external_id or info.url)
    except Exception:
        if platform == "vk":
            return multiplatform_metrics._normalize_vk_id(url)
    return ""


def _safe_url(platform: str, url: str) -> tuple[str, str] | None:
    url = _text(url)
    if not url:
        return None
    platform_id = _platform_id(platform, url)
    if not platform_id:
        return None
    if platform == "vk" and not re.search(r"(?:clip|video)-?\d+_\d+", url, re.I):
        return None
    return url, platform_id


def _eligible(video: dict[str, Any], now: datetime) -> bool:
    if _text(video.get("project_code")) != "vzyal_myach":
        return False
    published = _video_date(video)
    if published is None:
        return False
    age = (now.date() - published).days
    if age < -2 or age > RECENT_DAYS:
        return False
    return any(not _text(video.get(bot_url_field)) for _, bot_url_field, _ in PLATFORM_FIELDS.values())


def _known_keys(video: dict[str, Any]) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    for platform in PLATFORM_FIELDS:
        value = multiplatform_metrics._platform_id(video, platform)
        if value:
            keys.add((platform, value))
    return keys


def _source_rows(video: dict[str, Any], rows: list[dict[str, str]]) -> list[dict[str, str]]:
    known = _known_keys(video)
    found: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        for platform, (core_url_field, _, _) in PLATFORM_FIELDS.items():
            url = _text(row.get(core_url_field))
            platform_id = _platform_id(platform, url)
            if platform_id and (platform, platform_id) in known:
                key = _text(row.get("video_id")) or f"{platform}:{platform_id}"
                if key not in seen:
                    seen.add(key)
                    found.append(row)
                break
    return found


def _direct_additions(video: dict[str, Any], source_rows: list[dict[str, str]]) -> dict[str, tuple[str, str]]:
    additions: dict[str, tuple[str, str]] = {}
    for row in source_rows:
        for platform, (core_url_field, bot_url_field, _) in PLATFORM_FIELDS.items():
            if _text(video.get(bot_url_field)) or platform in additions:
                continue
            safe = _safe_url(platform, _text(row.get(core_url_field)))
            if safe:
                additions[platform] = safe
    return additions


def _candidate_rows(
    *,
    platform: str,
    video: dict[str, Any],
    source_rows: list[dict[str, str]],
    all_rows: list[dict[str, str]],
) -> list[dict[str, Any]]:
    published = _video_date(video)
    if published is None:
        return []
    reference_titles = [_text(row.get("Видео")) for row in source_rows if _text(row.get("Видео"))]
    if not reference_titles:
        return []
    source_duration = next((_int_or_none(row.get("duration_ms")) for row in source_rows if _int_or_none(row.get("duration_ms"))), None)
    core_url_field = PLATFORM_FIELDS[platform][0]
    ranked: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in all_rows:
        if _family(row.get("Проект")) != "vzyal_myach":
            continue
        url = _text(row.get(core_url_field))
        safe = _safe_url(platform, url)
        if safe is None:
            continue
        candidate_date = _row_date(row)
        if candidate_date is None:
            continue
        day_distance = _days(published, candidate_date)
        if day_distance > SUGGEST_MAX_DAYS:
            continue
        candidate_duration = _int_or_none(row.get("duration_ms"))
        duration_ok = _duration_compatible(source_duration, candidate_duration)
        title = _text(row.get("Видео"))
        best = (0.0, 0, 0.0, 0.0, "")
        for reference_title in reference_titles:
            score, common, jaccard, containment = _similarity(reference_title, title)
            if (score, common) > (best[0], best[1]):
                best = (score, common, jaccard, containment, reference_title)
        url_key = safe[1]
        if url_key in seen:
            continue
        seen.add(url_key)
        ranked.append({
            "row": row,
            "url": safe[0],
            "platform_id": safe[1],
            "title": title,
            "reference_title": best[4],
            "score": best[0],
            "common_tokens": best[1],
            "jaccard": best[2],
            "containment": best[3],
            "days": day_distance,
            "duration_ok": duration_ok,
            "duration_ms": candidate_duration,
            "project": _text(row.get("Проект")),
        })
    ranked.sort(
        key=lambda item: (
            1 if item["duration_ok"] else 0,
            item["score"],
            item["common_tokens"],
            -item["days"],
        ),
        reverse=True,
    )
    return ranked


def _auto_candidate(candidates: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, str]:
    if not candidates:
        return None, "unmatched"
    top = candidates[0]
    exact = bool(_norm(top["title"]) and _norm(top["title"]) == _norm(top["reference_title"]))
    qualifies = (
        top["duration_ok"]
        and top["days"] <= AUTO_MAX_DAYS
        and (
            exact
            or (top["score"] >= 0.82 and top["common_tokens"] >= 3)
            or (top["jaccard"] >= 0.70 and top["common_tokens"] >= 4)
        )
    )
    if not qualifies:
        return None, "suggested"
    if len(candidates) > 1:
        second = candidates[1]
        second_eligible = second["duration_ok"] and second["days"] <= AUTO_MAX_DAYS
        if second_eligible and abs(top["score"] - second["score"]) < AMBIGUITY_MARGIN:
            return None, "ambiguous"
    return top, "matched"


def _persist(video_id: int, additions: dict[str, tuple[str, str]]) -> int:
    if not additions:
        return 0
    assignments: list[str] = []
    params: list[Any] = []
    for platform, (url, platform_id) in additions.items():
        _, bot_url_field, bot_id_field = PLATFORM_FIELDS[platform]
        assignments.append(
            f"{bot_url_field} = CASE WHEN COALESCE({bot_url_field}, '') = '' THEN %s ELSE {bot_url_field} END"
        )
        params.append(url)
        if bot_id_field:
            assignments.append(
                f"{bot_id_field} = CASE WHEN COALESCE({bot_id_field}, '') = '' THEN %s ELSE {bot_id_field} END"
            )
            params.append(platform_id)
    assignments.append("updated_at = now()")
    params.append(video_id)
    with db.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE videos SET {', '.join(assignments)} WHERE id = %s", params)
            return int(cur.rowcount or 0)


def _suggestion(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": candidate["title"][:180],
        "reference_title": candidate["reference_title"][:180],
        "project": candidate["project"],
        "url": candidate["url"],
        "score": round(float(candidate["score"]), 3),
        "common_tokens": int(candidate["common_tokens"]),
        "jaccard": round(float(candidate["jaccard"]), 3),
        "days": int(candidate["days"]),
        "duration_ok": bool(candidate["duration_ok"]),
        "duration_ms": candidate["duration_ms"],
    }


def refresh(videos: list[dict[str, Any]]) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    targets = [video for video in videos if _eligible(video, now)]
    result: dict[str, Any] = {
        "videos_scanned": len(targets),
        "mirror_rows": 0,
        "source_found": 0,
        "direct_links_added": 0,
        "title_links_added": 0,
        "videos_updated": 0,
        "matched": 0,
        "ambiguous": 0,
        "suggested": 0,
        "unmatched": 0,
        "details": [],
    }
    if not targets:
        return result
    try:
        rows = _fetch_rows()
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"[:300]
        return result
    result["mirror_rows"] = len(rows)

    for video in targets:
        source = _source_rows(video, rows)
        detail: dict[str, Any] = {
            "video_id": int(video["id"]),
            "source_titles": [_text(row.get("Видео"))[:180] for row in source][:3],
            "added": {},
            "status": {},
            "suggestions": {},
        }
        if not source:
            detail["status"]["source"] = "not_found"
            result["details"].append(detail)
            continue
        result["source_found"] += 1

        additions = _direct_additions(video, source)
        for platform, value in additions.items():
            detail["added"][platform] = value[0]
            detail["status"][platform] = "same_core_group"
            result["direct_links_added"] += 1

        for platform, (_, bot_url_field, _) in PLATFORM_FIELDS.items():
            if _text(video.get(bot_url_field)) or platform in additions:
                continue
            candidates = _candidate_rows(
                platform=platform,
                video=video,
                source_rows=source,
                all_rows=rows,
            )
            chosen, status = _auto_candidate(candidates)
            detail["status"][platform] = status
            if chosen is not None:
                additions[platform] = (chosen["url"], chosen["platform_id"])
                detail["added"][platform] = chosen["url"]
                result["title_links_added"] += 1
                result["matched"] += 1
            else:
                if status == "ambiguous":
                    result["ambiguous"] += 1
                elif status == "suggested":
                    result["suggested"] += 1
                else:
                    result["unmatched"] += 1
                if candidates:
                    detail["suggestions"][platform] = [_suggestion(item) for item in candidates[:3]]

        if additions and _persist(int(video["id"]), additions):
            result["videos_updated"] += 1
        result["details"].append(detail)
    return result
