from __future__ import annotations

from typing import Any

from bot import sheet_layout
from bot.config import get_settings


PREAMBLE_LABELS = ["Что показывает", "Зачем нужна", "Важно"]


def _range(sheet_name: str, cells: str) -> str:
    escaped = sheet_name.replace("'", "''")
    return f"'{escaped}'!{cells}"


def _properties(service, spreadsheet_id: str) -> dict[str, dict[str, Any]]:
    payload = service.spreadsheets().get(
        spreadsheetId=spreadsheet_id,
        fields="sheets.properties",
    ).execute()
    return {
        str(sheet.get("properties", {}).get("title") or ""): sheet.get("properties", {})
        for sheet in payload.get("sheets", [])
    }


def _top_rows(service, spreadsheet_id: str, titles: list[str]) -> dict[str, list[list[Any]]]:
    if not titles:
        return {}
    payload = service.spreadsheets().values().batchGet(
        spreadsheetId=spreadsheet_id,
        ranges=[_range(title, "A1:AZ8") for title in titles],
        majorDimension="ROWS",
    ).execute()
    ranges = payload.get("valueRanges", [])
    return {
        title: (ranges[index].get("values", []) if index < len(ranges) else [])
        for index, title in enumerate(titles)
    }


def _has_preamble(rows: list[list[Any]]) -> bool:
    first = [str(row[0]).strip() if row else "" for row in rows[:3]]
    return first == PREAMBLE_LABELS


def _has_any_value(rows: list[list[Any]]) -> bool:
    return any(any(str(value).strip() for value in row) for row in rows)


def normalize_all_existing_sheet_preambles(*, service=None) -> dict[str, Any]:
    # Local import avoids a sheets -> sheet_preambles import cycle.
    from bot import sheets

    settings = get_settings()
    if not settings.google_sheets_spreadsheet_id:
        raise RuntimeError("GOOGLE_SHEETS_SPREADSHEET_ID is not configured")
    service = service or sheets._service()
    spreadsheet_id = settings.google_sheets_spreadsheet_id
    properties = _properties(service, spreadsheet_id)
    titles = [
        title for title in properties
        if title and not title.startswith("__tmp__") and not title.startswith("__old__")
    ]
    tables = _top_rows(service, spreadsheet_id, titles)

    inserted: list[str] = []
    already: list[str] = []
    empty: list[str] = []

    insert_requests: list[dict[str, Any]] = []
    for title in titles:
        rows = tables.get(title) or []
        if _has_preamble(rows):
            already.append(title)
            continue
        if not _has_any_value(rows):
            empty.append(title)
        else:
            insert_requests.append(
                {
                    "insertDimension": {
                        "range": {
                            "sheetId": int(properties[title]["sheetId"]),
                            "dimension": "ROWS",
                            "startIndex": 0,
                            "endIndex": sheet_layout.INFO_ROWS,
                        },
                        "inheritFromBefore": False,
                    }
                }
            )
        inserted.append(title)

    if insert_requests:
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": insert_requests},
        ).execute()

    updates: list[dict[str, Any]] = []
    for title in inserted:
        updates.append(
            {
                "range": _range(title, "A1:B3"),
                "values": sheet_layout.preamble_rows(title),
            }
        )
    if updates:
        service.spreadsheets().values().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"valueInputOption": "RAW", "data": updates},
        ).execute()

    # Apply the same visual treatment to every remaining tab. For previously
    # empty tabs there may be no row-4 header, but freezing 3/4 rows is harmless.
    format_requests: list[dict[str, Any]] = []
    for title in titles:
        sheet_id = int(properties[title]["sheetId"])
        format_requests.extend(
            [
                {
                    "repeatCell": {
                        "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 3},
                        "cell": {
                            "userEnteredFormat": {
                                "backgroundColor": {"red": 0.94, "green": 0.94, "blue": 0.94},
                                "wrapStrategy": "WRAP",
                                "verticalAlignment": "MIDDLE",
                            }
                        },
                        "fields": "userEnteredFormat(backgroundColor,wrapStrategy,verticalAlignment)",
                    }
                },
                {
                    "repeatCell": {
                        "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1},
                        "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                        "fields": "userEnteredFormat.textFormat.bold",
                    }
                },
                {
                    "updateSheetProperties": {
                        "properties": {
                            "sheetId": sheet_id,
                            "gridProperties": {
                                "frozenRowCount": 3 if title in empty else sheet_layout.HEADER_ROW
                            },
                        },
                        "fields": "gridProperties.frozenRowCount",
                    }
                },
            ]
        )
    if format_requests:
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": format_requests},
        ).execute()

    final_tables = _top_rows(service, spreadsheet_id, titles)
    failures = [title for title in titles if not _has_preamble(final_tables.get(title) or [])]
    return {
        "sheet_count": len(titles),
        "inserted": inserted,
        "already_present": already,
        "previously_empty": empty,
        "failures": failures,
        "titles": titles,
    }
