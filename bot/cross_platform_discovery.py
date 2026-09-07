from __future__ import annotations

import csv
import io
import re
from collections import defaultdict
from datetime import date, datetime, timezone
from typing import Any

import requests

from bot import db, multiplatform_metrics
from bot.links import normalize_instagram, normalize_tiktok, normalize_vk, normalize_youtube


PUBLICATIONS_MIRROR_SUFFIX = "/publications.tsv"
RECENT_DAYS = 60
MAX_DISTANCE_HOURS = 72.0
HIGH_SCORE = 0.88
NEAR_SCORE = 0.68
AMBIGUITY_MARGIN = 0.08
STOP_WORDS = {
    "подпишись", "подписывайся", "смотрим", "смотрите", "здесь", "живет", "живёт",
    "видео", "video", "shorts", "short", "reels", "reel", "credit", "copyright",
    "спасибо", "лайк", "лайки", "поставьте", "ставьте", "подробнее", "ссылка",
    "нашем", "наш", "канал", "канале", "vk", "instagram", "youtube", "tiktok",
}
PLATFORM_FIELDS = {
    "instagram": ("instagram_url", "instagram_id"),
    "youtube": ("youtube_url", "youtube_id"),
    "tiktok": ("tiktok_url", "tiktok_id"),
    "vk": ("vk_url", None),
}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _publications_url() -> str:
    bridge = multiplatform_metrics.CONTENT_CORE_BRIDGE_URL
    prefix, marker, rest = bridge.partition("/mirror/")
    if not marker or "/" not in rest:
        raise RuntimeError("Content Core bridge URL has unexpected format")
    token = rest.split("/", 1)[0]
    return f"{prefix.rstrip('/')}/mirror/{token}{PUBLICATIONS_MIRROR_SUFFIX}"


def _fetch_rows() -> list[dict[str, str]]:
    response = requests.get(_publications_url(), timeout=45)
    response.raise_for_status()
    return list(csv.DictReader(io.StringIO(response.text), delimiter="\t"))


def _family(value: Any) -> str:
    text = _text(value).lower().replace("ё", "е")
    text = re.sub(r"[^a-zа-я0-9]+", " ", text)
    if "взял мяч" in text or "vzyal myach" in text:
        return "vzyal_myach"
    return text.strip()


def _tokens(value: Any) -> list[str]:
    text = _text(value).lower().replace("ё", "е")
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"[@#][\w.-]+", " ", text, flags=re.UNICODE)
    text = re.sub(r"[^\w]+", " ", text, flags=re.UNICODE)
    return [
        token
        for token in text.split()
        if len(token) >= 3 and token not in STOP_WORDS
    ][:28]


def _normalized(value: Any) -> str:
    return " ".join(_tokens(value))


def _similarity(a: Any, b: Any) -> tuple[float, int]:
    left = set(_tokens(a))
    right = set(_tokens(b))
    if not left or not right:
        return 0.0, 0
    common = len(left & right)
    union = len(left | right)
    return (common / union if union else 0.0), common


def _dt(value: Any) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _video_dt(video: dict[str, Any]) -> datetime | None:
    value = video.get("publish_date")
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime(value.year, value.month, value.day)
    else:
        text = _text(value)
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text[:10])
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _hours(a: datetime, b: datetime) -> float:
    return abs((a - b).total_seconds()) / 3600.0


def _row_platform_id(row: dict[str, str]) -> str:
    platform = _text(row.get("platform"))
    url = _text(row.get("url"))
    external = _text(row.get("external_post_id"))
    if platform == "instagram":
        if url:
            try:
                value = normalize_instagram(url).external_id or ""
                if value:
                    return value
            except Exception:
                pass
        return external
    if platform == "youtube":
        if url:
            try:
                value = normalize_youtube(url).external_id or ""
                if value:
                    return value
            except Exception:
                pass
        return external
    if platform == "tiktok":
        if url:
            try:
                value = normalize_tiktok(url).external_id or ""
                if value:
                    return value
            except Exception:
                pass
        return external
    if platform == "vk":
        if url:
            try:
                info = normalize_vk(url)
                value = multiplatform_metrics._normalize_vk_id(info.external_id or info.url)
                if value:
                    return value
            except Exception:
                pass
        return multiplatform_metrics._normalize_vk_id(external)
    return ""


def _known_keys(video: dict[str, Any]) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    for platform in PLATFORM_FIELDS:
        value = multiplatform_metrics._platform_id(video, platform)
        if value:
            keys.add((platform, value))
    return keys


def _candidate_safe_url(platform: str, row: dict[str, str]) -> tuple[str, str] | None:
    url = _text(row.get("url"))
    if not url:
        return None
    platform_id = _row_platform_id(row)
    if not platform_id:
        return None
    if platform == "vk":
        # Do not turn a generic wall-post URL into a clip identity. Existing
        # Core VK data may still represent wall counters rather than clip counters.
        if not re.search(r"(?:clip|video)-?\d+_\d+", url, re.I):
            return None
    return url, platform_id


def _eligible_video(video: dict[str, Any], now: datetime) -> bool:
    if _text(video.get("project_code")) != "vzyal_myach":
        return False
    published = _video_dt(video)
    if published is None:
        return False
    age_days = (now - published).total_seconds() / 86400.0
    if age_days < -2 or age_days > RECENT_DAYS:
        return False
    return any(not _text(video.get(url_field)) for url_field, _ in PLATFORM_FIELDS.values())


def _source_rows(
    video: dict[str, Any],
    rows_by_key: dict[tuple[str, str], list[dict[str, str]]],
) -> list[dict[str, str]]:
    found: dict[str, dict[str, str]] = {}
    for key in _known_keys(video):
        for row in rows_by_key.get(key, []):
            found[_text(row.get("publication_id")) or f"{key[0]}:{key[1]}"] = row
    return list(found.values())


def _match_score(reference_captions: list[str], candidate_caption: str) -> tuple[float, int, bool]:
    best_score = 0.0
    best_common = 0
    exact = False
    candidate_norm = _normalized(candidate_caption)
    for reference in reference_captions:
        reference_norm = _normalized(reference)
        if reference_norm and candidate_norm and reference_norm == candidate_norm and len(reference_norm) >= 12:
            exact = True
            best_score = 1.0
            best_common = max(best_common, len(set(reference_norm.split())))
            continue
        score, common = _similarity(reference, candidate_caption)
        if score > best_score or (score == best_score and common > best_common):
            best_score = score
            best_common = common
    return best_score, best_common, exact


def _choose_candidate(
    *,
    platform: str,
    video: dict[str, Any],
    source_rows: list[dict[str, str]],
    all_rows: list[dict[str, str]],
) -> tuple[dict[str, str] | None, str]:
    published = _video_dt(video)
    if published is None:
        return None, "no_publish_date"
    family = _family(video.get("project_name") or video.get("project_code"))
    references = [_text(row.get("caption")) for row in source_rows if _text(row.get("caption"))]
    if not references:
        return None, "no_reference_caption"

    candidates: dict[str, tuple[dict[str, str], float, float, int, bool]] = {}
    for row in all_rows:
        if _text(row.get("platform")) != platform:
            continue
        if _family(row.get("project")) != family:
            continue
        candidate_dt = _dt(row.get("published_at"))
        if candidate_dt is None:
            continue
        distance = _hours(published, candidate_dt)
        if distance > MAX_DISTANCE_HOURS:
            continue
        safe = _candidate_safe_url(platform, row)
        if safe is None:
            continue
        _, platform_id = safe
        score, common, exact = _match_score(references, _text(row.get("caption")))
        qualifies = exact or score >= HIGH_SCORE or (score >= NEAR_SCORE and common >= 3)
        if not qualifies:
            continue
        previous = candidates.get(platform_id)
        item = (row, score, distance, common, exact)
        if previous is None or (score, -distance, common) > (previous[1], -previous[2], previous[3]):
            candidates[platform_id] = item

    ranked = sorted(
        candidates.values(),
        key=lambda item: (1 if item[4] else 0, item[1], item[3], -item[2]),
        reverse=True,
    )
    if not ranked:
        return None, "unmatched"
    if len(ranked) > 1:
        top = ranked[0]
        second = ranked[1]
        top_rank = (1 if top[4] else 0, top[1])
        second_rank = (1 if second[4] else 0, second[1])
        if top_rank[0] == second_rank[0] and abs(top_rank[1] - second_rank[1]) < AMBIGUITY_MARGIN:
            return None, "ambiguous"
    return ranked[0][0], "matched"


def _persist(video_id: int, additions: dict[str, tuple[str, str]]) -> int:
    if not additions:
        return 0
    assignments: list[str] = []
    params: list[Any] = []
    for platform, (url, platform_id) in additions.items():
        url_field, id_field = PLATFORM_FIELDS[platform]
        assignments.append(f"{url_field} = CASE WHEN COALESCE({url_field}, '') = '' THEN %s ELSE {url_field} END")
        params.append(url)
        if id_field:
            assignments.append(f"{id_field} = CASE WHEN COALESCE({id_field}, '') = '' THEN %s ELSE {id_field} END")
            params.append(platform_id)
    assignments.append("updated_at = now()")
    params.append(video_id)
    sql = f"UPDATE videos SET {', '.join(assignments)} WHERE id = %s"
    with db.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return int(cur.rowcount or 0)


def refresh(videos: list[dict[str, Any]]) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    targets = [video for video in videos if _eligible_video(video, now)]
    result: dict[str, Any] = {
        "videos_scanned": len(targets),
        "links_added": 0,
        "videos_updated": 0,
        "matched": 0,
        "ambiguous": 0,
        "unmatched": 0,
        "no_source": 0,
        "mirror_rows": 0,
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
    rows_by_key: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        platform = _text(row.get("platform"))
        if platform not in PLATFORM_FIELDS:
            continue
        platform_id = _row_platform_id(row)
        if platform_id:
            rows_by_key[(platform, platform_id)].append(row)

    for video in targets:
        source = _source_rows(video, rows_by_key)
        detail: dict[str, Any] = {"video_id": int(video["id"]), "added": {}, "status": {}}
        if not source:
            result["no_source"] += 1
            detail["status"]["source"] = "not_found"
            result["details"].append(detail)
            continue

        additions: dict[str, tuple[str, str]] = {}
        for platform, (url_field, _) in PLATFORM_FIELDS.items():
            if _text(video.get(url_field)):
                continue
            candidate, status = _choose_candidate(
                platform=platform,
                video=video,
                source_rows=source,
                all_rows=rows,
            )
            detail["status"][platform] = status
            if status == "ambiguous":
                result["ambiguous"] += 1
            elif status != "matched":
                result["unmatched"] += 1
            if candidate is None:
                continue
            safe = _candidate_safe_url(platform, candidate)
            if safe is None:
                continue
            additions[platform] = safe
            detail["added"][platform] = safe[0]
            result["matched"] += 1

        if additions:
            changed = _persist(int(video["id"]), additions)
            if changed:
                result["videos_updated"] += 1
                result["links_added"] += len(additions)
        result["details"].append(detail)

    return result
