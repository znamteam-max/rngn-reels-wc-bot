from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from typing import Any

from bot import multiplatform_metrics, reporting_sheet_v2, sheets
from bot.config import get_settings


SHEET_NAME = "👤 Авторы"
COLUMNS = [
    "Проект",
    "Автор",
    "Username",
    "Работ",
    "Всего просмотров",
    "Максимум одного видео",
    "ID лучшего",
    "Дата лучшего",
    "Тип лучшего",
    "Покрытие",
]
DESCRIPTION = (
    "Итог по авторам активных проектов: количество approved-работ, сумма известных "
    "просмотров и максимум одной работы. ЧМ 2026 исключён."
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _as_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = _text(value)
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _coverage(
    videos: list[dict[str, Any]],
    latest: dict[tuple[int, str], dict[str, Any]],
) -> str:
    parts: list[str] = []
    for platform, label in (
        ("instagram", "IG"),
        ("youtube", "YT"),
        ("tiktok", "TT"),
        ("vk", "VK"),
    ):
        supplied = sum(
            1 for video in videos if multiplatform_metrics._platform_id(video, platform)
        )
        measured = sum(
            1 for video in videos if (int(video["id"]), platform) in latest
        )
        parts.append(f"{label} {measured}/{supplied}")
    return " · ".join(parts)


def build_rows(
    videos: list[dict[str, Any]],
    latest: dict[tuple[int, str], dict[str, Any]],
) -> list[list[Any]]:
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for video in reporting_sheet_v2.active_reporting_videos(videos):
        name, username = reporting_sheet_v2._person_parts(video, "author")
        if not name and not username:
            continue
        project_code = reporting_sheet_v2._project_code(video)
        groups[(project_code, name, username)].append(video)

    prepared: list[tuple[int, int, int, str, list[Any]]] = []
    for (project_code, name, username), items in groups.items():
        scored: list[tuple[int, date, int, dict[str, Any]]] = []
        total_views = 0
        for video in items:
            metric = multiplatform_metrics._video_metric_values(video, latest)
            views = int(metric.get("total_known_views") or 0)
            total_views += views
            scored.append(
                (
                    views,
                    _as_date(video.get("publish_date")) or date.min,
                    int(video.get("id") or 0),
                    video,
                )
            )
        scored.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
        best_views, _, best_id, best_video = scored[0]
        author = name or f"@{username}"
        project_name = reporting_sheet_v2._project_name(items[0])
        row = [
            project_name,
            author,
            f"@{username}" if username else "",
            len(items),
            total_views,
            best_views,
            best_id,
            _text(best_video.get("publish_date")),
            reporting_sheet_v2._work_type(best_video),
            _coverage(items, latest),
        ]
        prepared.append(
            (
                total_views,
                len(items),
                reporting_sheet_v2.PROJECT_ORDER.get(project_code, 999),
                author.casefold(),
                row,
            )
        )

    prepared.sort(key=lambda item: (-item[0], -item[1], item[2], item[3]))
    return [item[-1] for item in prepared]


def _dimension_request(sheet_id: int, start: int, end: int, pixels: int) -> dict[str, Any]:
    return {
        "updateDimensionProperties": {
            "range": {
                "sheetId": sheet_id,
                "dimension": "COLUMNS",
                "startIndex": start,
                "endIndex": end,
            },
            "properties": {"pixelSize": pixels},
            "fields": "pixelSize",
        }
    }


def _target_sheet_index(properties: dict[str, dict[str, Any]]) -> int:
    # The author summary is the entry point for payroll review and must stay
    # the leftmost visible tab even after automated reporting rebuilds.
    return 0


def sync(videos: list[dict[str, Any]], *, service=None) -> dict[str, int]:
    settings = get_settings()
    if not settings.google_sheets_spreadsheet_id:
        raise RuntimeError("GOOGLE_SHEETS_SPREADSHEET_ID is not configured")
    service = service or sheets._service()
    spreadsheet_id = settings.google_sheets_spreadsheet_id
    latest = multiplatform_metrics._latest_snapshots()
    rows = build_rows(videos, latest)

    sheets._ensure_named_sheets(service, spreadsheet_id, {SHEET_NAME: COLUMNS})
    sheets._replace_named_sheet(service, spreadsheet_id, SHEET_NAME, COLUMNS, rows)
    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=sheets._sheet_range(SHEET_NAME, "A1:B1"),
        valueInputOption="RAW",
        body={"values": [["О вкладке", DESCRIPTION]]},
    ).execute()

    properties = sheets._sheet_properties(service, spreadsheet_id)
    item = properties.get(SHEET_NAME)
    if item:
        sheet_id = int(item["sheetId"])
        target_index = _target_sheet_index(properties)
        requests: list[dict[str, Any]] = [
            {"clearBasicFilter": {"sheetId": sheet_id}},
            {
                "setBasicFilter": {
                    "filter": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": 1,
                            "endRowIndex": max(2, len(rows) + 2),
                            "startColumnIndex": 0,
                            "endColumnIndex": len(COLUMNS),
                        }
                    }
                }
            },
            {
                "updateSheetProperties": {
                    "properties": {
                        "sheetId": sheet_id,
                        "hidden": False,
                        "index": target_index,
                        "gridProperties": {"frozenRowCount": 2},
                    },
                    "fields": "hidden,index,gridProperties.frozenRowCount",
                }
            },
            {
                "repeatCell": {
                    "range": {"sheetId": sheet_id},
                    "cell": {"userEnteredFormat": {"wrapStrategy": "OVERFLOW_CELL"}},
                    "fields": "userEnteredFormat.wrapStrategy",
                }
            },
            {
                "updateDimensionProperties": {
                    "range": {
                        "sheetId": sheet_id,
                        "dimension": "ROWS",
                        "startIndex": 0,
                        "endIndex": 1,
                    },
                    "properties": {"pixelSize": 28},
                    "fields": "pixelSize",
                }
            },
            _dimension_request(sheet_id, 0, 1, 165),
            _dimension_request(sheet_id, 1, 3, 175),
            _dimension_request(sheet_id, 3, 6, 140),
            _dimension_request(sheet_id, 6, 7, 90),
            _dimension_request(sheet_id, 7, 9, 125),
            _dimension_request(sheet_id, 9, 10, 245),
        ]
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": requests},
        ).execute()

    return {
        "rows": len(rows),
        "authors": len({(row[0], row[1], row[2]) for row in rows}),
        "projects": len({row[0] for row in rows}),
    }
