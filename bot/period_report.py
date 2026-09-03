from __future__ import annotations

import csv
import io
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any

from bot import author_reports, db, multiplatform_metrics
from bot.config import get_settings
from bot.telegram import TelegramClient


COMMANDS = {"/period_report", "/report_period", "/export_period"}
PLATFORMS = ("instagram", "youtube", "tiktok", "vk")
PLATFORM_SHORT = {
    "instagram": "IG",
    "youtube": "YT",
    "tiktok": "TT",
    "vk": "VK",
}

CSV_COLUMNS = [
    "video_id",
    "date",
    "project_code",
    "project_name",
    "work_type",
    "author",
    "author_username",
    "montage",
    "montage_username",
    "voice",
    "voice_username",
    "instagram_url",
    "instagram_views",
    "youtube_url",
    "youtube_views",
    "tiktok_url",
    "tiktok_views",
    "vk_url",
    "vk_views",
    "total_known_views",
    "coverage",
    "metrics_status",
    "content_core_status",
    "content_core_video_id",
    "content_core_publication_ids",
]


def _parse_date(value: str) -> date:
    text = value.strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    raise ValueError(text)


def _month_bounds(today: date) -> tuple[date, date]:
    start = today.replace(day=1)
    if today.month == 12:
        next_month = date(today.year + 1, 1, 1)
    else:
        next_month = date(today.year, today.month + 1, 1)
    return start, next_month - timedelta(days=1)


def parse_period(rest: str, *, today: date | None = None) -> tuple[date, date]:
    today = today or datetime.now(get_settings().tz).date()
    if not rest.strip():
        return _month_bounds(today)
    parts = [part for part in rest.replace("—", " ").replace("–", " ").split() if part]
    if len(parts) != 2:
        raise ValueError("expected two dates")
    start = _parse_date(parts[0])
    end = _parse_date(parts[1])
    if end < start:
        start, end = end, start
    if (end - start).days > 730:
        raise ValueError("period too large")
    return start, end


def _as_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _work_type(video: dict[str, Any]) -> str:
    if str(video.get("video_type") or "").lower() == "bigrecap":
        return "bigrecap"
    if "отрез из эфира" in str(video.get("comment") or "").casefold():
        return "aircut"
    return "reel"


def _person(video: dict[str, Any], role: str) -> tuple[str, str]:
    return (
        str(video.get(f"{role}_name") or "").strip(),
        str(video.get(f"{role}_username") or "").strip().lstrip("@"),
    )


def _core_links(video_ids: list[int]) -> tuple[dict[int, dict[str, Any]], dict[int, list[str]]]:
    if not video_ids:
        return {}, {}
    try:
        video_links = {
            int(row["video_id"]): row
            for row in db.fetch_all(
                """
                SELECT video_id, content_core_video_id, resolve_status
                FROM content_core_video_links
                WHERE video_id = ANY(%s)
                """,
                (video_ids,),
            )
        }
        publication_rows = db.fetch_all(
            """
            SELECT video_id, content_core_publication_id
            FROM content_core_publication_links
            WHERE video_id = ANY(%s)
            ORDER BY video_id, content_core_publication_id
            """,
            (video_ids,),
        )
    except Exception:
        return {}, {}
    publications: dict[int, list[str]] = defaultdict(list)
    for row in publication_rows:
        video_id = int(row["video_id"])
        publication_id = str(row.get("content_core_publication_id") or "").strip()
        if publication_id and publication_id not in publications[video_id]:
            publications[video_id].append(publication_id)
    return video_links, dict(publications)


def _new_author_totals() -> dict[str, int]:
    totals = {"reels": 0, "known": 0}
    for platform in PLATFORMS:
        totals[platform] = 0
        totals[f"{platform}_supplied"] = 0
        totals[f"{platform}_measured"] = 0
    return totals


def _platform_summary(totals: dict[str, int], platform: str) -> str:
    short = PLATFORM_SHORT[platform]
    supplied = totals[f"{platform}_supplied"]
    measured = totals[f"{platform}_measured"]
    if supplied == 0:
        return f"{short} —"
    if measured == 0:
        return f"{short} missing 0/{supplied}"
    views = f"{totals[platform]:,}".replace(",", " ")
    suffix = "" if measured == supplied else f" ({measured}/{supplied})"
    return f"{short} {views}{suffix}"


def build_export(start: date, end: date) -> tuple[list[dict[str, Any]], str, bytes]:
    videos = [
        video
        for video in author_reports._active_videos()
        if str(video.get("status") or "") == "approved"
        and (published := _as_date(video.get("publish_date"))) is not None
        and start <= published <= end
    ]
    videos.sort(key=lambda item: (_as_date(item.get("publish_date")) or date.max, int(item["id"])))
    latest = multiplatform_metrics._latest_snapshots()
    video_ids = [int(video["id"]) for video in videos]
    core_video_links, core_publication_links = _core_links(video_ids)

    rows: list[dict[str, Any]] = []
    author_totals: dict[tuple[str, str], dict[str, int]] = defaultdict(_new_author_totals)

    for video in videos:
        video_id = int(video["id"])
        metric = multiplatform_metrics._video_metric_values(video, latest)
        supplied = 0
        measured = 0
        row: dict[str, Any] = {
            "video_id": video_id,
            "date": str(video.get("publish_date") or ""),
            "project_code": str(video.get("project_code") or ""),
            "project_name": str(video.get("project_name") or ""),
            "work_type": _work_type(video),
            "author": _person(video, "author")[0],
            "author_username": _person(video, "author")[1],
            "montage": _person(video, "montage")[0],
            "montage_username": _person(video, "montage")[1],
            "voice": _person(video, "voice")[0],
            "voice_username": _person(video, "voice")[1],
        }
        for platform in PLATFORMS:
            platform_url = str(video.get(f"{platform}_url") or "").strip()
            snapshot = latest.get((video_id, platform))
            if platform_url:
                supplied += 1
            if snapshot:
                measured += 1
            row[f"{platform}_url"] = platform_url
            row[f"{platform}_views"] = "" if snapshot is None else int(snapshot.get("views") or 0)
        row["total_known_views"] = int(metric["total_known_views"])
        row["coverage"] = multiplatform_metrics._coverage(video, latest)
        row["metrics_status"] = "complete" if measured == supplied else "partial"
        core_link = core_video_links.get(video_id, {})
        row["content_core_status"] = str(core_link.get("resolve_status") or "unresolved")
        row["content_core_video_id"] = str(core_link.get("content_core_video_id") or "")
        row["content_core_publication_ids"] = ",".join(core_publication_links.get(video_id, []))
        rows.append(row)

        author = _person(video, "author")
        key = author if author[0] or author[1] else ("Без автора", "")
        totals = author_totals[key]
        totals["reels"] += 1
        totals["known"] += int(metric["total_known_views"])
        for platform in PLATFORMS:
            platform_url = str(video.get(f"{platform}_url") or "").strip()
            snapshot = latest.get((video_id, platform))
            if platform_url:
                totals[f"{platform}_supplied"] += 1
            if snapshot is not None:
                totals[f"{platform}_measured"] += 1
                totals[platform] += int(snapshot.get("views") or 0)

    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=CSV_COLUMNS, extrasaction="ignore", delimiter=";")
    writer.writeheader()
    writer.writerows(rows)
    csv_bytes = ("\ufeff" + output.getvalue()).encode("utf-8")

    lines = [
        f"📊 ОТЧЁТ {start.strftime('%d.%m.%Y')}–{end.strftime('%d.%m.%Y')}",
        f"Одобренных работ: {len(rows)}",
        "",
        "По авторам: текущие известные просмотры работ, опубликованных в выбранном периоде.",
    ]
    if not author_totals:
        lines.append("Нет одобренных работ за этот период.")
    else:
        for (name, username), totals in sorted(
            author_totals.items(), key=lambda item: (-item[1]["known"], item[0][0].casefold())
        ):
            label = f"{name} (@{username})" if username else name
            known = f"{totals['known']:,}".replace(",", " ")
            platform_text = " · ".join(_platform_summary(totals, platform) for platform in PLATFORMS)
            lines.append(f"• {label}: {totals['reels']} работ · {known} известных просмотров\n  {platform_text}")
    lines.extend(
        [
            "",
            "В CSV одна строка = одна работа. Пустая platform-метрика означает missing, а не 0.",
        ]
    )
    return rows, "\n".join(lines), csv_bytes


def handle_message(message: dict[str, Any]) -> bool:
    text = str(message.get("text") or "").strip()
    if not text.startswith("/"):
        return False
    command_token, _, rest = text.partition(" ")
    command = command_token.split("@", 1)[0].lower()
    if command not in COMMANDS:
        return False

    from bot import handlers as h

    actor = h._actor_from_message(message)
    if not actor:
        return True
    tg = TelegramClient(timeout=20)
    if not h.require_admin(tg, actor):
        return True

    try:
        start, end = parse_period(rest)
    except ValueError:
        tg.send_message(
            actor.chat_id,
            "Формат: /period_report DD.MM.YYYY DD.MM.YYYY\n"
            "Например: /period_report 01.09.2026 30.09.2026\n"
            "Без дат команда выгружает текущий месяц.",
        )
        return True

    rows, summary, csv_bytes = build_export(start, end)
    tg.send_message(actor.chat_id, summary)
    filename = f"reels-report_{start.isoformat()}_{end.isoformat()}.csv"
    tg.send_document_bytes(
        actor.chat_id,
        filename,
        csv_bytes,
        caption=f"Reels Report · {start.strftime('%d.%m.%Y')}–{end.strftime('%d.%m.%Y')} · {len(rows)} работ",
    )
    return True
