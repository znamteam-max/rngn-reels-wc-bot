from __future__ import annotations

from typing import Any

from bot import reporting_sheet_v2, sheets
from bot.config import get_settings


URL_COLUMNS_BY_SHEET = {
    "project": (6, 8, 10, 12),
    "report": (7, 9, 11, 13),
}


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


def finalize_layout(*, service=None) -> dict[str, int]:
    settings = get_settings()
    if not settings.google_sheets_spreadsheet_id:
        raise RuntimeError("GOOGLE_SHEETS_SPREADSHEET_ID is not configured")
    service = service or sheets._service()
    spreadsheet_id = settings.google_sheets_spreadsheet_id
    properties = sheets._sheet_properties(service, spreadsheet_id)
    requests: list[dict[str, Any]] = []

    stale = [
        item
        for title, item in properties.items()
        if title.startswith("__tmp__r") or title.startswith("__old__r")
    ]
    for item in stale:
        requests.append({"deleteSheet": {"sheetId": int(item["sheetId"])}})

    project_titles = set(reporting_sheet_v2.PROJECT_TAB_BY_CODE.values())
    for title in project_titles:
        item = properties.get(title)
        if not item:
            continue
        sheet_id = int(item["sheetId"])
        requests.extend(
            [
                _dimension_request(sheet_id, 0, 1, 95),
                _dimension_request(sheet_id, 1, 2, 65),
                _dimension_request(sheet_id, 2, 3, 135),
                _dimension_request(sheet_id, 3, 6, 190),
                _dimension_request(sheet_id, 6, 14, 155),
                _dimension_request(sheet_id, 14, 17, 135),
            ]
        )

    report = properties.get(reporting_sheet_v2.REPORT_SHEET)
    if report:
        sheet_id = int(report["sheetId"])
        requests.extend(
            [
                _dimension_request(sheet_id, 0, 1, 95),
                _dimension_request(sheet_id, 1, 2, 155),
                _dimension_request(sheet_id, 2, 4, 175),
                _dimension_request(sheet_id, 4, 6, 125),
                _dimension_request(sheet_id, 6, 7, 65),
                _dimension_request(sheet_id, 7, 15, 150),
                _dimension_request(sheet_id, 15, 17, 135),
            ]
        )

    summary = properties.get(reporting_sheet_v2.PROJECTS_SHEET)
    if summary:
        sheet_id = int(summary["sheetId"])
        requests.extend(
            [
                _dimension_request(sheet_id, 0, 1, 95),
                _dimension_request(sheet_id, 1, 2, 165),
                _dimension_request(sheet_id, 2, 7, 115),
                _dimension_request(sheet_id, 7, 13, 135),
            ]
        )

    if requests:
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": requests},
        ).execute()
    return {"stale_tabs_deleted": len(stale), "layout_requests": len(requests)}
