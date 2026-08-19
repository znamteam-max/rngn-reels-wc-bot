from __future__ import annotations

import csv
import io
import json
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from typing import Any

import requests
from psycopg.types.json import Jsonb

from bot import db, sheets, youtube_metrics
from bot.config import get_settings
from bot.links import normalize_instagram, normalize_tiktok, normalize_vk, normalize_youtube

CONTENT_CORE_BRIDGE_URL = (
    "https://rngn-content-core.rngn-znamteam.workers.dev/"
    "mirror/gE3-bKEV9YF5KeOTnJEzs9iCp1ZAcaZ60KgNyvgtknU/bot-metrics.tsv"
)
PLATFORMS = ("instagram", "youtube", "tiktok", "vk")
CORE_PLATFORMS = ("instagram", "tiktok", "vk")
YOUTUBE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
YOUTUBE_ID_PREFIX_RE = re.compile(r"^([A-Za-z0-9_-]{11})(?:$|[^A-Za-z0-9_-])")
VK_NUMERIC_ID_RE = re.compile(r"(-?\d+_\d+)")
AUTHOR_VIEWS_SHEET = "Просмотры авторов"
REEL_VIEWS_SHEET = "Просмотры — ролики"

AUTHOR_COLUMNS = [
    "period",
    "project_code",
    "project_name",
    "rank_known",
    "author",
    "reels",
    "instagram_posts",
    "instagram_measured",
    "instagram_views",
    "youtube_posts",
    "youtube_measured",
    "youtube_views",
    "tiktok_posts",
    "tiktok_measured",
    "tiktok_views",
    "vk_posts",
    "vk_measured",
    "vk_views",
    "total_known_views",
    "avg_known_views_per_reel",
    "coverage",
    "data_complete",
    "best_video_id",
    "best_video_views",
    "updated_at",
]

REEL_COLUMNS = [
    "video_id",
    "publish_date",
    "publish_month",
    "project_code",
    "project_name",
    "author",
    "instagram_views",
    "youtube_views",
    "tiktok_views",
    "vk_views",
    "total_known_views",
    "coverage",
    "metrics_updated_at",
    "instagram_id",
    "youtube_id",
    "tiktok_id",
    "vk_id",
]


@dataclass
class SyncSummary:
    core_rows: int = 0
    core_matched: int = 0
    core_unmatched: int = 0
    core_conflicts: int = 0
    core_skipped_vk_wall: int = 0
    snapshots_written: int = 0
    youtube_videos: int = 0
    youtube_success: int = 0
    youtube_missing: int = 0
    youtube_errors: int = 0
    youtube_views: int = 0
    author_rows: int = 0
    reel_rows: int = 0

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def _text(value: Any) -> str:
    return str(value or "").strip()


def _int_or_none(value: Any) -> int | None:
    text = _text(value)
    if not text:
        return None
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


def _iso_datetime(value: Any) -> datetime | None:
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


def _day_bounds(captured_at: datetime) -> tuple[datetime, datetime]:
    local_day = captured_at.astimezone(get_settings().tz).date()
    start = datetime.combine(local_day, time.min, tzinfo=get_settings().tz)
    end = datetime.combine(local_day, time.max, tzinfo=get_settings().tz)
    return start, end


def _normalize_vk_id(value: str) -> str:
    match = VK_NUMERIC_ID_RE.search(value)
    return match.group(1) if match else ""


def _resolve_youtube_id(video: dict[str, Any]) -> str:
    direct = _text(video.get("youtube_id"))
    if YOUTUBE_ID_RE.fullmatch(direct):
        return direct
    url = _text(video.get("youtube_url"))
    if url:
        try:
            value = normalize_youtube(url).external_id or ""
        except Exception:
            value = ""
        if YOUTUBE_ID_RE.fullmatch(value):
            return value
    prefix = YOUTUBE_ID_PREFIX_RE.match(direct)
    return prefix.group(1) if prefix else ""


def _platform_id(video: dict[str, Any], platform: str) -> str:
    direct = _text(video.get(f"{platform}_id"))
    url = _text(video.get(f"{platform}_url"))
    if platform == "instagram":
        if direct:
            return direct
        if url:
            try:
                return normalize_instagram(url).external_id or ""
            except Exception:
                return ""
    if platform == "youtube":
        return _resolve_youtube_id(video)
    if platform == "tiktok":
        if direct:
            return direct
        if url:
            try:
                return normalize_tiktok(url).external_id or ""
            except Exception:
                return ""
    if platform == "vk":
        if direct:
            normalized = _normalize_vk_id(direct)
            if normalized:
                return normalized
        if url:
            try:
                info = normalize_vk(url)
                normalized = _normalize_vk_id(info.external_id or info.url)
                if normalized:
                    return normalized
            except Exception:
                return _normalize_vk_id(url)
    return ""


def _approved_videos() -> list[dict[str, Any]]:
    return db.fetch_all(
        """
        SELECT
            v.*,
            COALESCE(NULLIF(v.author_name, ''), author_p.name, 'Без автора') AS resolved_author,
            COALESCE(NULLIF(v.author_username, ''), author_p.username) AS resolved_author_username
        FROM videos v
        LEFT JOIN people author_p ON author_p.id = v.author_id
        WHERE v.status = 'approved'
        ORDER BY v.publish_date NULLS LAST, v.id
        """
    )


def _build_exact_index(videos: list[dict[str, Any]]) -> tuple[dict[tuple[str, str], dict[str, Any]], set[tuple[str, str]]]:
    buckets: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for video in videos:
        for platform in PLATFORMS:
            platform_id = _platform_id(video, platform)
            if platform_id:
                buckets[(platform, platform_id)].append(video)
    exact: dict[tuple[str, str], dict[str, Any]] = {}
    conflicts: set[tuple[str, str]] = set()
    for key, items in buckets.items():
        if len(items) == 1:
            exact[key] = items[0]
        else:
            conflicts.add(key)
    return exact, conflicts


def _upsert_snapshot(
    video: dict[str, Any],
    *,
    platform: str,
    platform_video_id: str,
    platform_url: str | None,
    captured_at: datetime,
    views: int | None,
    likes: int | None,
    comments: int | None,
    shares: int | None,
    raw_data: dict[str, Any],
) -> None:
    day_start, day_end = _day_bounds(captured_at)
    with db.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id
                FROM video_metrics_snapshots
                WHERE video_id = %s
                  AND platform = %s
                  AND captured_at >= %s
                  AND captured_at <= %s
                ORDER BY captured_at DESC, id DESC
                LIMIT 1
                """,
                (int(video["id"]), platform, day_start, day_end),
            )
            existing = cur.fetchone()
            values = (
                platform_video_id,
                platform_url,
                captured_at,
                views,
                likes,
                comments,
                shares,
                "ok",
                None,
                Jsonb(raw_data),
            )
            if existing:
                cur.execute(
                    """
                    UPDATE video_metrics_snapshots
                    SET platform_video_id=%s,
                        platform_url=%s,
                        captured_at=%s,
                        views=%s,
                        likes=%s,
                        comments=%s,
                        shares=%s,
                        source_status=%s,
                        error_message=%s,
                        raw_data=%s
                    WHERE id=%s
                    """,
                    (*values, int(existing["id"])),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO video_metrics_snapshots (
                        video_id, platform, platform_video_id, platform_url,
                        captured_at, views, likes, comments, shares,
                        source_status, error_message, raw_data
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (int(video["id"]), platform, *values),
                )
            if platform == "youtube":
                cur.execute(
                    """
                    UPDATE videos
                    SET youtube_id=%s,
                        youtube_views=%s,
                        youtube_likes=%s,
                        youtube_comments=%s,
                        youtube_last_sync_at=%s,
                        updated_at=now()
                    WHERE id=%s
                    """,
                    (
                        platform_video_id,
                        views,
                        likes,
                        comments,
                        captured_at,
                        int(video["id"]),
                    ),
                )


def _fetch_core_rows() -> list[dict[str, str]]:
    response = requests.get(CONTENT_CORE_BRIDGE_URL, timeout=40)
    response.raise_for_status()
    return list(csv.DictReader(io.StringIO(response.text), delimiter="\t"))


def sync_content_core_platforms(videos: list[dict[str, Any]], summary: SyncSummary) -> None:
    exact, conflicts = _build_exact_index(videos)
    rows = _fetch_core_rows()
    summary.core_rows = len(rows)
    for row in rows:
        platform = _text(row.get("platform"))
        if platform not in CORE_PLATFORMS:
            continue
        match_id = _text(row.get("match_id"))
        if platform == "vk":
            match_id = _normalize_vk_id(match_id)
            # Historical Core VK snapshots are wall-post counters. Only import
            # snapshots created by the corrected clip collector.
            if _text(row.get("metric_source")) != "video":
                summary.core_skipped_vk_wall += 1
                continue
        key = (platform, match_id)
        if not match_id:
            continue
        if key in conflicts:
            summary.core_conflicts += 1
            continue
        video = exact.get(key)
        if not video:
            summary.core_unmatched += 1
            continue
        captured_at = _iso_datetime(row.get("captured_at"))
        if captured_at is None:
            continue
        _upsert_snapshot(
            video,
            platform=platform,
            platform_video_id=match_id,
            platform_url=_text(row.get("url")) or None,
            captured_at=captured_at,
            views=_int_or_none(row.get("views")),
            likes=_int_or_none(row.get("likes")),
            comments=_int_or_none(row.get("comments")),
            shares=_int_or_none(row.get("shares")),
            raw_data={
                "source": "rngn_content_core",
                "publication_id": row.get("publication_id"),
                "project_code": row.get("project_code"),
                "metric_source": row.get("metric_source"),
            },
        )
        summary.core_matched += 1
        summary.snapshots_written += 1


def sync_live_youtube(videos: list[dict[str, Any]], summary: SyncSummary) -> None:
    key = get_settings().youtube_api_key
    if not key:
        summary.youtube_errors += 1
        return
    by_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for video in videos:
        youtube_id = _platform_id(video, "youtube")
        if youtube_id:
            by_id[youtube_id].append(video)
    summary.youtube_videos = sum(len(items) for items in by_id.values())
    captured_at = datetime.now(timezone.utc)
    ids = list(by_id)
    for offset in range(0, len(ids), 50):
        batch = ids[offset : offset + 50]
        try:
            stats_by_id, missing = youtube_metrics.fetch_youtube_statistics(batch, key)
        except Exception:
            summary.youtube_errors += len(batch)
            continue
        summary.youtube_missing += len(missing)
        for youtube_id, stats in stats_by_id.items():
            for video in by_id[youtube_id]:
                _upsert_snapshot(
                    video,
                    platform="youtube",
                    platform_video_id=youtube_id,
                    platform_url=_text(video.get("youtube_url")) or None,
                    captured_at=captured_at,
                    views=stats.views,
                    likes=stats.likes,
                    comments=stats.comments,
                    shares=None,
                    raw_data={"source": "youtube_api", "item": stats.raw_data},
                )
                summary.youtube_success += 1
                summary.youtube_views += int(stats.views or 0)
                summary.snapshots_written += 1


def _latest_snapshots() -> dict[tuple[int, str], dict[str, Any]]:
    rows = db.fetch_all(
        """
        SELECT DISTINCT ON (video_id, platform)
            video_id, platform, platform_video_id, captured_at,
            views, likes, comments, shares, raw_data
        FROM video_metrics_snapshots
        WHERE source_status = 'ok'
          AND platform IN ('instagram','youtube','tiktok','vk')
        ORDER BY video_id, platform, captured_at DESC, id DESC
        """
    )
    return {(int(row["video_id"]), str(row["platform"])): row for row in rows}


def _month(value: Any) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m")
    if isinstance(value, date):
        return value.strftime("%Y-%m")
    text = _text(value)
    return text[:7] if len(text) >= 7 else "Без даты"


def _coverage(video: dict[str, Any], latest: dict[tuple[int, str], dict[str, Any]]) -> str:
    parts: list[str] = []
    for platform, label in (("instagram", "IG"), ("youtube", "YT"), ("tiktok", "TT"), ("vk", "VK")):
        supplied = 1 if _platform_id(video, platform) else 0
        measured = 1 if (int(video["id"]), platform) in latest else 0
        parts.append(f"{label} {measured}/{supplied}")
    return " · ".join(parts)


def _video_metric_values(video: dict[str, Any], latest: dict[tuple[int, str], dict[str, Any]]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    total = 0
    updated: list[datetime] = []
    for platform in PLATFORMS:
        snapshot = latest.get((int(video["id"]), platform))
        views = int(snapshot.get("views") or 0) if snapshot else 0
        values[f"{platform}_views"] = views if snapshot else None
        total += views
        if snapshot and isinstance(snapshot.get("captured_at"), datetime):
            updated.append(snapshot["captured_at"])
    values["total_known_views"] = total
    values["metrics_updated_at"] = max(updated).isoformat() if updated else ""
    return values


def build_reel_rows(videos: list[dict[str, Any]], latest: dict[tuple[int, str], dict[str, Any]]) -> list[list[str]]:
    result: list[list[str]] = []
    for video in sorted(videos, key=lambda item: (_text(item.get("project_code")), _text(item.get("publish_date")), int(item["id"]))):
        metric = _video_metric_values(video, latest)
        row = {
            "video_id": int(video["id"]),
            "publish_date": _text(video.get("publish_date")),
            "publish_month": _month(video.get("publish_date")),
            "project_code": _text(video.get("project_code")) or "unassigned",
            "project_name": _text(video.get("project_name")) or "Без проекта",
            "author": _text(video.get("resolved_author")) or "Без автора",
            **metric,
            "coverage": _coverage(video, latest),
            "instagram_id": _platform_id(video, "instagram"),
            "youtube_id": _platform_id(video, "youtube"),
            "tiktok_id": _platform_id(video, "tiktok"),
            "vk_id": _platform_id(video, "vk"),
        }
        result.append([_text(row.get(column)) for column in REEL_COLUMNS])
    return result


def _author_group(
    videos: list[dict[str, Any]],
    latest: dict[tuple[int, str], dict[str, Any]],
    *,
    period: str,
    project_code: str,
    project_name: str,
    author: str,
) -> dict[str, Any]:
    platform_stats: dict[str, dict[str, int]] = {}
    totals_by_video: list[tuple[int, int]] = []
    total_views = 0
    complete = True
    for platform in PLATFORMS:
        posts = sum(1 for video in videos if _platform_id(video, platform))
        measured = sum(1 for video in videos if (int(video["id"]), platform) in latest)
        views = sum(int((latest.get((int(video["id"]), platform)) or {}).get("views") or 0) for video in videos)
        platform_stats[platform] = {"posts": posts, "measured": measured, "views": views}
        if measured < posts:
            complete = False
        total_views += views
    for video in videos:
        metric = _video_metric_values(video, latest)
        totals_by_video.append((int(video["id"]), int(metric["total_known_views"])))
    totals_by_video.sort(key=lambda item: (-item[1], item[0]))
    best_id, best_views = totals_by_video[0] if totals_by_video else (None, 0)
    coverage = " · ".join(
        f"{label} {platform_stats[platform]['measured']}/{platform_stats[platform]['posts']}"
        for platform, label in (("instagram", "IG"), ("youtube", "YT"), ("tiktok", "TT"), ("vk", "VK"))
    )
    return {
        "period": period,
        "project_code": project_code,
        "project_name": project_name,
        "rank_known": "",
        "author": author,
        "reels": len(videos),
        "instagram_posts": platform_stats["instagram"]["posts"],
        "instagram_measured": platform_stats["instagram"]["measured"],
        "instagram_views": platform_stats["instagram"]["views"],
        "youtube_posts": platform_stats["youtube"]["posts"],
        "youtube_measured": platform_stats["youtube"]["measured"],
        "youtube_views": platform_stats["youtube"]["views"],
        "tiktok_posts": platform_stats["tiktok"]["posts"],
        "tiktok_measured": platform_stats["tiktok"]["measured"],
        "tiktok_views": platform_stats["tiktok"]["views"],
        "vk_posts": platform_stats["vk"]["posts"],
        "vk_measured": platform_stats["vk"]["measured"],
        "vk_views": platform_stats["vk"]["views"],
        "total_known_views": total_views,
        "avg_known_views_per_reel": round(total_views / len(videos)) if videos else 0,
        "coverage": coverage,
        "data_complete": "YES" if complete else "NO",
        "best_video_id": best_id or "",
        "best_video_views": best_views,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def build_author_rows(videos: list[dict[str, Any]], latest: dict[tuple[int, str], dict[str, Any]]) -> list[list[str]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    project_names: dict[str, str] = {}
    for video in videos:
        project_code = _text(video.get("project_code")) or "unassigned"
        project_names[project_code] = _text(video.get("project_name")) or "Без проекта"
        groups[(project_code, "ALL")].append(video)
        groups[(project_code, _month(video.get("publish_date")))].append(video)

    rows: list[dict[str, Any]] = []
    for (project_code, period), project_videos in groups.items():
        by_author: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for video in project_videos:
            by_author[_text(video.get("resolved_author")) or "Без автора"].append(video)
        author_rows = [
            _author_group(
                author_videos,
                latest,
                period=period,
                project_code=project_code,
                project_name=project_names[project_code],
                author=author,
            )
            for author, author_videos in by_author.items()
        ]
        ranked = sorted(author_rows, key=lambda row: (-int(row["total_known_views"]), str(row["author"]).casefold()))
        for rank, row in enumerate(ranked, start=1):
            row["rank_known"] = rank
            rows.append(row)
        all_row = _author_group(
            project_videos,
            latest,
            period=period,
            project_code=project_code,
            project_name=project_names[project_code],
            author="ALL",
        )
        all_row["rank_known"] = ""
        rows.append(all_row)

    def sort_key(row: dict[str, Any]):
        project_priority = 0 if row["project_code"] == "world_cup_2026" else 1
        period_priority = 0 if row["period"] == "ALL" else 1
        all_priority = 0 if row["author"] == "ALL" else 1
        return (
            project_priority,
            str(row["project_name"]),
            period_priority,
            str(row["period"]),
            all_priority,
            int(row["rank_known"] or 0),
            str(row["author"]),
        )

    rows.sort(key=sort_key)
    return [[_text(row.get(column)) for column in AUTHOR_COLUMNS] for row in rows]


def sync_view_sheets(videos: list[dict[str, Any]], summary: SyncSummary) -> None:
    settings = get_settings()
    if not settings.google_sheets_spreadsheet_id:
        raise RuntimeError("GOOGLE_SHEETS_SPREADSHEET_ID is not configured")
    latest = _latest_snapshots()
    author_rows = build_author_rows(videos, latest)
    reel_rows = build_reel_rows(videos, latest)
    service = sheets._service()
    spreadsheet_id = settings.google_sheets_spreadsheet_id
    sheets._ensure_named_sheets(
        service,
        spreadsheet_id,
        {
            AUTHOR_VIEWS_SHEET: AUTHOR_COLUMNS,
            REEL_VIEWS_SHEET: REEL_COLUMNS,
        },
    )
    sheets._replace_named_sheet(service, spreadsheet_id, AUTHOR_VIEWS_SHEET, AUTHOR_COLUMNS, author_rows)
    sheets._replace_named_sheet(service, spreadsheet_id, REEL_VIEWS_SHEET, REEL_COLUMNS, reel_rows)
    values = service.spreadsheets().values()
    values.update(
        spreadsheetId=spreadsheet_id,
        range=f"'{AUTHOR_VIEWS_SHEET}'!A1:B1",
        valueInputOption="RAW",
        body={"values": [["О вкладке", "Просмотры по авторам: сумма известных метрик по платформам. Missing ≠ 0; смотри coverage и data_complete."]]},
    ).execute()
    values.update(
        spreadsheetId=spreadsheet_id,
        range=f"'{REEL_VIEWS_SHEET}'!A1:B1",
        valueInputOption="RAW",
        body={"values": [["О вкладке", "Один ролик = одна строка; просмотры по платформам и известная сумма. Missing ≠ 0."]]},
    ).execute()
    summary.author_rows = len(author_rows)
    summary.reel_rows = len(reel_rows)


def sync_multiplatform_metrics() -> dict[str, Any]:
    summary = SyncSummary()
    videos = _approved_videos()
    sync_content_core_platforms(videos, summary)
    sync_live_youtube(videos, summary)
    sync_view_sheets(videos, summary)
    try:
        with db.transaction() as conn:
            db.log_event(
                conn,
                entity_type="metrics",
                entity_id=None,
                action="multiplatform_metrics_sync",
                actor_username="system",
                after_data=summary.to_dict(),
            )
    except Exception:
        pass
    return {"ok": True, "approved_videos": len(videos), **summary.to_dict()}
