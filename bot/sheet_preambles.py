from __future__ import annotations

import re
from typing import Any

from bot import sheet_layout
from bot.config import get_settings


OLD_PREAMBLE_LABELS = ["Что показывает", "Зачем нужна", "Важно"]
NEW_PREAMBLE_LABEL = "О вкладке"
MONTH_RE = re.compile(r"^\d{4}-(?:0[1-9]|1[0-2])$")

# Lower number = further to the left among filled tabs.
IMPORTANT_FILLED_ORDER = {
    "Работа авторов": 0,
    "ЧМ 2026": 1,
    "Метрики": 2,
    "Монтаж — справочно": 3,
    "Videos": 20,
    "Project Stats": 30,
    "Month Stats": 31,
    "Reconciliation": 40,
    "MetricsRaw": 50,
    "Project Backfill Review": 60,
    "Unfinished Requests": 70,
    "Unsubmitted Forms": 71,
}

# Empty tabs always go after every filled tab. This only controls their order
# inside the empty block.
EMPTY_ORDER = {
    "Весь Спорт": 0,
    "Без даты": 10,
    "Unfinished Requests": 20,
    "Unsubmitted Forms": 21,
    "Project Backfill Review": 22,
    "People": 90,
    "Drafts": 91,
    "Batches": 92,
    "Logs": 93,
    "Stats": 94,
}


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


def _tables(
    service,
    spreadsheet_id: str,
    titles: list[str],
    *,
    cells: str = "A1:AZ1000",
) -> dict[str, list[list[Any]]]:
    if not titles:
        return {}
    payload = service.spreadsheets().values().batchGet(
        spreadsheetId=spreadsheet_id,
        ranges=[_range(title, cells) for title in titles],
        majorDimension="ROWS",
    ).execute()
    ranges = payload.get("valueRanges", [])
    return {
        title: (ranges[index].get("values", []) if index < len(ranges) else [])
        for index, title in enumerate(titles)
    }


def _first_cells(rows: list[list[Any]], count: int) -> list[str]:
    return [str(row[0]).strip() if row else "" for row in rows[:count]]


def _preamble_kind(rows: list[list[Any]]) -> str:
    if _first_cells(rows, 3) == OLD_PREAMBLE_LABELS:
        return "old_three_rows"
    if rows and rows[0] and str(rows[0][0]).strip() == NEW_PREAMBLE_LABEL:
        return "one_row"
    return "none"


def _has_any_value(rows: list[list[Any]]) -> bool:
    return any(any(str(value).strip() for value in row) for row in rows)


def _has_data_rows(rows: list[list[Any]]) -> bool:
    # After normalization row 1 is the explanation and row 2 is the header on
    # managed tabs. A tab is considered filled only if something exists below
    # that header. This keeps header-only pages in the empty block at the end.
    return _has_any_value(rows[2:])


def _filled_sort_key(title: str, original_index: int) -> tuple[int, int, str]:
    if MONTH_RE.match(title):
        # Filled month tabs are important and are ordered newest first directly
        # after the three human-facing summary tabs.
        return (10, -int(title.replace("-", "")), title)
    return (IMPORTANT_FILLED_ORDER.get(title, 80), original_index, title.casefold())


def _empty_sort_key(title: str, original_index: int) -> tuple[int, int, str]:
    if MONTH_RE.match(title):
        return (5, -int(title.replace("-", "")), title)
    return (EMPTY_ORDER.get(title, 50), original_index, title.casefold())


def _reorder_tabs(
    service,
    spreadsheet_id: str,
    properties: dict[str, dict[str, Any]],
    filled: list[str],
    empty: list[str],
) -> list[str]:
    original_index = {
        title: int(props.get("index") or 0)
        for title, props in properties.items()
    }
    filled_sorted = sorted(
        filled,
        key=lambda title: _filled_sort_key(title, original_index.get(title, 999)),
    )
    empty_sorted = sorted(
        empty,
        key=lambda title: _empty_sort_key(title, original_index.get(title, 999)),
    )
    desired = [*filled_sorted, *empty_sorted]
    requests = [
        {
            "updateSheetProperties": {
                "properties": {
                    "sheetId": int(properties[title]["sheetId"]),
                    "index": index,
                },
                "fields": "index",
            }
        }
        for index, title in enumerate(desired)
    ]
    if requests:
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": requests},
        ).execute()
    return desired


def normalize_all_existing_sheet_preambles(*, service=None) -> dict[str, Any]:
    """Convert every visible tab to one plain-language explanation row.

    Old three-row explanations are collapsed safely by deleting only rows 2-3,
    so the existing row-4 header becomes row 2 and all data shifts up intact.
    Tabs without any explanation get one new row inserted at the top. Empty tabs
    receive the explanation without creating fake data rows.
    """
    # Local import avoids a sheets -> sheet_preambles import cycle.
    from bot import sheets

    settings = get_settings()
    if not settings.google_sheets_spreadsheet_id:
        raise RuntimeError("GOOGLE_SHEETS_SPREADSHEET_ID is not configured")
    service = service or sheets._service()
    spreadsheet_id = settings.google_sheets_spreadsheet_id
    properties = _properties(service, spreadsheet_id)
    titles = [
        title
        for title, props in sorted(
            properties.items(),
            key=lambda item: int(item[1].get("index") or 0),
        )
        if title
        and not bool(props.get("hidden"))
        and not title.startswith("__tmp__")
        and not title.startswith("__old__")
    ]
    before = _tables(service, spreadsheet_id, titles, cells="A1:AZ8")

    collapsed: list[str] = []
    inserted: list[str] = []
    already_one_line: list[str] = []
    initially_empty: list[str] = []
    dimension_requests: list[dict[str, Any]] = []

    for title in titles:
        rows = before.get(title) or []
        kind = _preamble_kind(rows)
        if kind == "old_three_rows":
            collapsed.append(title)
            dimension_requests.append(
                {
                    "deleteDimension": {
                        "range": {
                            "sheetId": int(properties[title]["sheetId"]),
                            "dimension": "ROWS",
                            "startIndex": 1,
                            "endIndex": 3,
                        }
                    }
                }
            )
            continue
        if kind == "one_row":
            already_one_line.append(title)
            continue
        if not _has_any_value(rows):
            initially_empty.append(title)
        else:
            dimension_requests.append(
                {
                    "insertDimension": {
                        "range": {
                            "sheetId": int(properties[title]["sheetId"]),
                            "dimension": "ROWS",
                            "startIndex": 0,
                            "endIndex": 1,
                        },
                        "inheritFromBefore": False,
                    }
                }
            )
        inserted.append(title)

    if dimension_requests:
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": dimension_requests},
        ).execute()

    # Rewrite row 1 on every tab, including already-normalized tabs, so wording
    # always stays current when descriptions are improved in code.
    service.spreadsheets().values().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={
            "valueInputOption": "RAW",
            "data": [
                {
                    "range": _range(title, "A1:B1"),
                    "values": sheet_layout.preamble_rows(title),
                }
                for title in titles
            ],
        },
    ).execute()

    properties = _properties(service, spreadsheet_id)
    format_requests: list[dict[str, Any]] = []
    for title in titles:
        sheet_id = int(properties[title]["sheetId"])
        format_requests.extend(
            [
                {
                    "repeatCell": {
                        "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1},
                        "cell": {
                            "userEnteredFormat": {
                                "backgroundColor": {"red": 0.94, "green": 0.94, "blue": 0.94},
                                "textFormat": {"bold": True},
                                "wrapStrategy": "WRAP",
                                "verticalAlignment": "MIDDLE",
                            }
                        },
                        "fields": "userEnteredFormat(backgroundColor,textFormat.bold,wrapStrategy,verticalAlignment)",
                    }
                },
                {
                    "repeatCell": {
                        "range": {"sheetId": sheet_id, "startRowIndex": 1, "endRowIndex": 2},
                        "cell": {
                            "userEnteredFormat": {
                                "backgroundColor": {"red": 0.86, "green": 0.89, "blue": 0.93},
                                "textFormat": {"bold": True},
                                "wrapStrategy": "WRAP",
                            }
                        },
                        "fields": "userEnteredFormat(backgroundColor,textFormat.bold,wrapStrategy)",
                    }
                },
                {
                    "updateSheetProperties": {
                        "properties": {
                            "sheetId": sheet_id,
                            "gridProperties": {"frozenRowCount": sheet_layout.HEADER_ROW},
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

    full_tables = _tables(service, spreadsheet_id, titles)
    failures = [
        title
        for title in titles
        if _preamble_kind(full_tables.get(title) or []) != "one_row"
    ]
    filled = [title for title in titles if _has_data_rows(full_tables.get(title) or [])]
    empty = [title for title in titles if title not in filled]
    desired_order = _reorder_tabs(service, spreadsheet_id, properties, filled, empty)

    final_properties = _properties(service, spreadsheet_id)
    actual_order = [
        title
        for title, props in sorted(
            final_properties.items(),
            key=lambda item: int(item[1].get("index") or 0),
        )
        if title in set(titles)
    ]
    return {
        "sheet_count": len(titles),
        "collapsed_three_row_descriptions": collapsed,
        "inserted_one_row_descriptions": inserted,
        "already_one_line": already_one_line,
        "initially_empty": initially_empty,
        "filled_titles": filled,
        "empty_titles": empty,
        "desired_order": desired_order,
        "actual_order": actual_order,
        "order_matches": actual_order == desired_order,
        "failures": failures,
    }
