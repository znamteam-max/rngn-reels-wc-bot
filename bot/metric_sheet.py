from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from bot import db, sheets
from bot.config import get_settings


SUMMARY_SHEET_NAME = "Метрики"
RAW_SHEET_NAME = "MetricsRaw"
SUMMARY_COLUMNS = [
    "Видео",
    "Просмотры сейчас",
    "Лайки",
    "Комментарии",
    "+ просмотров за 1 день",
    "+ за 7 дней",
    "+ за 30 дней",
    "Обновлено",
]


def _local_datetime(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(get_settings().tz)


def _metric_value(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _approved_youtube_videos() -> list[dict[str, Any]]:
    return db.fetch_all(
        """
        SELECT id, youtube_url, youtube_id
        FROM videos
        WHERE status = 'approved'
          AND youtube_url IS NOT NULL
          AND btrim(youtube_url) <> ''
        ORDER BY id ASC
        """
    )


def _successful_youtube_snapshots() -> list[dict[str, Any]]:
    return db.fetch_all(
        """
        SELECT
            s.id,
            s.video_id,
            s.captured_at,
            s.views,
            s.likes,
            s.comments,
            s.platform_url,
            s.platform_video_id,
            v.youtube_url,
            v.youtube_id
        FROM video_metrics_snapshots s
        JOIN videos v ON v.id = s.video_id
        WHERE s.platform = 'youtube'
          AND s.source_status = 'ok'
          AND v.status = 'approved'
          AND v.youtube_url IS NOT NULL
          AND btrim(v.youtube_url) <> ''
        ORDER BY s.video_id ASC, s.captured_at ASC, s.id ASC
        """
    )


def build_summary_rows() -> list[list[Any]]:
    videos = _approved_youtube_videos()
    snapshots = _successful_youtube_snapshots()

    history: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for snapshot in snapshots:
        history[int(snapshot["video_id"])].append(snapshot)

    rows_with_sort: list[tuple[int, int, list[Any]]] = []
    for video in videos:
        video_id = int(video["id"])
        items = history.get(video_id, [])
        video_url = str(video.get("youtube_url") or "").strip() or f"YouTube ID {video_id}"

        if not items:
            rows_with_sort.append(
                (
                    1,
                    0,
                    [video_url, "", "", "", "", "", "", "—"],
                )
            )
            continue

        latest = items[-1]
        latest_local = _local_datetime(latest.get("captured_at"))
        if latest_local is None:
            rows_with_sort.append(
                (
                    1,
                    0,
                    [video_url, "", "", "", "", "", "", "—"],
                )
            )
            continue

        # The collector keeps at most one successful snapshot per video per local
        # calendar day. If a sync ran more than once, keep the latest one that day.
        by_day: dict[Any, dict[str, Any]] = {}
        for item in items:
            local_dt = _local_datetime(item.get("captured_at"))
            if local_dt is not None:
                by_day[local_dt.date()] = item

        current_views = _metric_value(latest.get("views"))
        current_likes = _metric_value(latest.get("likes"))
        current_comments = _metric_value(latest.get("comments"))

        def view_delta(days: int) -> int | str:
            baseline = by_day.get(latest_local.date() - timedelta(days=days))
            if baseline is None or current_views is None:
                return "—"
            baseline_views = _metric_value(baseline.get("views"))
            if baseline_views is None:
                return "—"
            return current_views - baseline_views

        row = [
            video_url,
            current_views if current_views is not None else "",
            current_likes if current_likes is not None else "",
            current_comments if current_comments is not None else "",
            view_delta(1),
            view_delta(7),
            view_delta(30),
            latest_local.strftime("%d.%m.%Y %H:%M"),
        ]
        rows_with_sort.append((0, -(current_views or 0), row))

    rows_with_sort.sort(key=lambda item: (item[0], item[1], str(item[2][0]).casefold()))
    return [item[2] for item in rows_with_sort]


def _format_summary_sheet(service, spreadsheet_id: str) -> None:
    properties = sheets._sheet_properties(service, spreadsheet_id).get(SUMMARY_SHEET_NAME)
    if not properties:
        return
    sheet_id = int(properties["sheetId"])
    service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={
            "requests": [
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": 2,
                            "startColumnIndex": 1,
                            "endColumnIndex": 7,
                        },
                        "cell": {
                            "userEnteredFormat": {
                                "numberFormat": {"type": "NUMBER", "pattern": "#,##0"}
                            }
                        },
                        "fields": "userEnteredFormat.numberFormat",
                    }
                },
                {
                    "updateDimensionProperties": {
                        "range": {
                            "sheetId": sheet_id,
                            "dimension": "COLUMNS",
                            "startIndex": 0,
                            "endIndex": 1,
                        },
                        "properties": {"pixelSize": 360},
                        "fields": "pixelSize",
                    }
                },
                {
                    "updateDimensionProperties": {
                        "range": {
                            "sheetId": sheet_id,
                            "dimension": "COLUMNS",
                            "startIndex": 1,
                            "endIndex": 7,
                        },
                        "properties": {"pixelSize": 150},
                        "fields": "pixelSize",
                    }
                },
                {
                    "updateDimensionProperties": {
                        "range": {
                            "sheetId": sheet_id,
                            "dimension": "COLUMNS",
                            "startIndex": 7,
                            "endIndex": 8,
                        },
                        "properties": {"pixelSize": 145},
                        "fields": "pixelSize",
                    }
                },
            ]
        },
    ).execute()


def hide_raw_sheet(*, service=None) -> bool:
    settings = get_settings()
    if not settings.google_sheets_spreadsheet_id:
        raise RuntimeError("GOOGLE_SHEETS_SPREADSHEET_ID is not configured")
    service = service or sheets._service()
    spreadsheet_id = settings.google_sheets_spreadsheet_id
    properties = sheets._sheet_properties(service, spreadsheet_id)
    raw = properties.get(RAW_SHEET_NAME)
    if not raw:
        return False
    if bool(raw.get("hidden")):
        return True
    service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={
            "requests": [
                {
                    "updateSheetProperties": {
                        "properties": {"sheetId": int(raw["sheetId"]), "hidden": True},
                        "fields": "hidden",
                    }
                }
            ]
        },
    ).execute()
    return True


def refresh_metric_summary(*, service=None) -> int:
    settings = get_settings()
    if not settings.google_sheets_spreadsheet_id:
        raise RuntimeError("GOOGLE_SHEETS_SPREADSHEET_ID is not configured")
    service = service or sheets._service()
    spreadsheet_id = settings.google_sheets_spreadsheet_id
    rows = build_summary_rows()
    sheets._ensure_named_sheets(
        service,
        spreadsheet_id,
        {SUMMARY_SHEET_NAME: SUMMARY_COLUMNS},
    )
    sheets._replace_named_sheet(
        service,
        spreadsheet_id,
        SUMMARY_SHEET_NAME,
        SUMMARY_COLUMNS,
        rows,
    )
    _format_summary_sheet(service, spreadsheet_id)
    hide_raw_sheet(service=service)
    return len(rows)
