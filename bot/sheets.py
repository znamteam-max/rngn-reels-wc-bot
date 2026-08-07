from __future__ import annotations

import base64
import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from google.oauth2 import service_account
from googleapiclient.discovery import build

from bot import reconciliation
from bot.config import get_settings
from bot.messages import person_value
from bot.projects import PROJECTS, PROJECT_SHEET_TITLES, REPORTING_PROJECTS, project_sheet_title


SHEET_NAME = "Videos"
METRICS_SHEET_NAME = "MetricsRaw"
PROJECT_STATS_SHEET_NAME = "Project Stats"
PEOPLE_PROJECTS_SHEET_NAME = "People × Projects"
MONTH_STATS_SHEET_NAME = reconciliation.MONTH_STATS_SHEET_NAME
UNFINISHED_SHEET_NAME = reconciliation.UNFINISHED_SHEET_NAME
UNSUBMITTED_SHEET_NAME = reconciliation.UNSUBMITTED_SHEET_NAME
RECONCILIATION_SHEET_NAME = reconciliation.RECONCILIATION_SHEET_NAME
BACKFILL_REVIEW_SHEET_NAME = reconciliation.BACKFILL_REVIEW_SHEET_NAME
SHEET_COLUMNS = [
    "id",
    "status",
    "video_type",
    "project_id",
    "project_code",
    "project_name",
    "publish_date",
    "instagram_url",
    "instagram_id",
    "youtube_url",
    "youtube_id",
    "tiktok_url",
    "vk_url",
    "author",
    "author_tg_id",
    "montage",
    "montage_tg_id",
    "voice",
    "voice_tg_id",
    "added_by",
    "checked_by",
    "created_at",
    "checked_at",
    "batch_id",
    "comment",
    *reconciliation.DERIVED_VIDEO_COLUMNS,
]
PROJECT_STATS_COLUMNS = reconciliation.PROJECT_STATS_COLUMNS
MONTH_STATS_COLUMNS = reconciliation.MONTH_STATS_COLUMNS
PEOPLE_PROJECTS_COLUMNS = reconciliation.PEOPLE_PROJECTS_COLUMNS
METRICS_COLUMNS = [
    "captured_at",
    "video_id",
    "platform",
    "platform_video_id",
    "views",
    "likes",
    "comments",
    "shares",
    "source_status",
    "error_message",
    "instagram_url",
    "youtube_url",
    "author",
    "montage",
    "voice",
]


def _service():
    settings = get_settings()
    if not settings.google_service_account_json_b64:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON_B64 is not configured")
    raw = base64.b64decode(settings.google_service_account_json_b64)
    info = json.loads(raw.decode("utf-8"))
    credentials = service_account.Credentials.from_service_account_info(
        info,
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    return build("sheets", "v4", credentials=credentials, cache_discovery=False)


def _as_cell(value: Any) -> str:
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _user_cell(username: str | None, tg_id: int | None) -> str:
    if username:
        return f"@{username}"
    return str(tg_id or "")


def _video_type_cell(value: Any) -> str:
    return "bigrecap" if value == "bigrecap" else "regular"


def _column_letter(column_count: int) -> str:
    letters = ""
    while column_count:
        column_count, remainder = divmod(column_count - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def video_to_row(video: dict[str, Any], columns: list[str] | None = None) -> list[str]:
    values = {
        "id": video.get("id"),
        "status": video.get("status"),
        "video_type": _video_type_cell(video.get("video_type")),
        "project_id": video.get("project_id"),
        "project_code": video.get("project_code"),
        "project_name": video.get("project_name"),
        "publish_date": video.get("publish_date"),
        "instagram_url": video.get("instagram_url"),
        "instagram_id": video.get("instagram_id"),
        "youtube_url": video.get("youtube_url"),
        "youtube_id": video.get("youtube_id"),
        "tiktok_url": video.get("tiktok_url"),
        "vk_url": video.get("vk_url"),
        "author": person_value(video, "author"),
        "author_tg_id": video.get("author_tg_id"),
        "montage": person_value(video, "montage"),
        "montage_tg_id": video.get("montage_tg_id"),
        "voice": person_value(video, "voice") if video.get("voice_name") else "",
        "voice_tg_id": video.get("voice_tg_id"),
        "added_by": _user_cell(video.get("added_by_username"), video.get("added_by_tg_id")),
        "checked_by": _user_cell(video.get("checked_by_username"), video.get("checked_by_tg_id")),
        "created_at": video.get("created_at"),
        "checked_at": video.get("checked_at"),
        "batch_id": video.get("batch_id"),
        "comment": video.get("comment"),
        **reconciliation.derived_video_values(video),
    }
    return [_as_cell(values.get(column)) for column in (columns or SHEET_COLUMNS)]


def _sheet_properties(service, spreadsheet_id: str) -> dict[str, dict[str, Any]]:
    spreadsheet = (
        service.spreadsheets()
        .get(spreadsheetId=spreadsheet_id, fields="sheets.properties")
        .execute()
    )
    return {
        sheet.get("properties", {}).get("title", ""): sheet.get("properties", {})
        for sheet in spreadsheet.get("sheets", [])
    }


def _video_sheet_header(service, spreadsheet_id: str) -> list[str]:
    result = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=f"{SHEET_NAME}!1:1")
        .execute()
    )
    values = result.get("values", [])
    if not values:
        return []
    return [str(value).strip() for value in values[0]]


def _write_video_header(service, spreadsheet_id: str, columns: list[str]) -> None:
    end_column = _column_letter(len(columns))
    (
        service.spreadsheets()
        .values()
        .update(
            spreadsheetId=spreadsheet_id,
            range=f"{SHEET_NAME}!A1:{end_column}1",
            valueInputOption="USER_ENTERED",
            body={"values": [columns]},
        )
        .execute()
    )


def _ensure_video_sheet_columns(service, spreadsheet_id: str) -> list[str]:
    properties = _sheet_properties(service, spreadsheet_id)
    if SHEET_NAME not in properties:
        (
            service.spreadsheets()
            .batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={"requests": [{"addSheet": {"properties": {"title": SHEET_NAME}}}]},
            )
            .execute()
        )
        _write_video_header(service, spreadsheet_id, SHEET_COLUMNS)
        return SHEET_COLUMNS

    header = _video_sheet_header(service, spreadsheet_id)
    if not header:
        _write_video_header(service, spreadsheet_id, SHEET_COLUMNS)
        return SHEET_COLUMNS

    if "video_type" not in header:
        status_index = header.index("status") if "status" in header else 1
        insert_index = status_index + 1
        sheet_id = properties[SHEET_NAME]["sheetId"]
        (
            service.spreadsheets()
            .batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={
                    "requests": [
                        {
                            "insertDimension": {
                                "range": {
                                    "sheetId": sheet_id,
                                    "dimension": "COLUMNS",
                                    "startIndex": insert_index,
                                    "endIndex": insert_index + 1,
                                },
                                "inheritFromBefore": True,
                            }
                        }
                    ]
                },
            )
            .execute()
        )
        header = header[:insert_index] + ["video_type"] + header[insert_index:]
        _write_video_header(service, spreadsheet_id, header)

    columns = [column for column in header if column]
    changed = False
    for column in SHEET_COLUMNS:
        if column not in columns:
            columns.append(column)
            changed = True
    if changed:
        _write_video_header(service, spreadsheet_id, columns)
    return columns


def _sheet_range(sheet_name: str, cells: str) -> str:
    return f"'{sheet_name.replace(chr(39), chr(39) * 2)}'!{cells}"


def _find_row_by_id(
    service,
    spreadsheet_id: str,
    video_id: int,
    sheet_name: str = SHEET_NAME,
) -> int | None:
    result = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=_sheet_range(sheet_name, "A2:A"))
        .execute()
    )
    for index, row in enumerate(result.get("values", []), start=2):
        if row and str(row[0]) == str(video_id):
            return index
    return None


def _write_named_sheet_header(
    service,
    spreadsheet_id: str,
    sheet_name: str,
    columns: list[str],
) -> None:
    end_column = _column_letter(len(columns))
    (
        service.spreadsheets()
        .values()
        .update(
            spreadsheetId=spreadsheet_id,
            range=_sheet_range(sheet_name, f"A1:{end_column}1"),
            valueInputOption="USER_ENTERED",
            body={"values": [columns]},
        )
        .execute()
    )


def _ensure_named_sheets(
    service,
    spreadsheet_id: str,
    sheets_with_columns: dict[str, list[str]],
) -> None:
    properties = _sheet_properties(service, spreadsheet_id)
    missing = [title for title in sheets_with_columns if title not in properties]
    if missing:
        (
            service.spreadsheets()
            .batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={
                    "requests": [
                        {"addSheet": {"properties": {"title": title}}}
                        for title in missing
                    ]
                },
            )
            .execute()
        )
    for title in missing:
        _write_named_sheet_header(service, spreadsheet_id, title, sheets_with_columns[title])


def _clear_video_from_named_sheet(
    service,
    spreadsheet_id: str,
    sheet_name: str,
    video_id: int,
    column_count: int,
) -> None:
    row_number = _find_row_by_id(service, spreadsheet_id, video_id, sheet_name)
    if not row_number:
        return
    end_column = _column_letter(column_count)
    (
        service.spreadsheets()
        .values()
        .clear(
            spreadsheetId=spreadsheet_id,
            range=_sheet_range(sheet_name, f"A{row_number}:{end_column}{row_number}"),
            body={},
        )
        .execute()
    )


def _upsert_video_in_named_sheet(
    service,
    spreadsheet_id: str,
    sheet_name: str,
    video: dict[str, Any],
    columns: list[str],
) -> int:
    row_number = _find_row_by_id(service, spreadsheet_id, int(video["id"]), sheet_name)
    return _write_video_to_named_sheet(
        service,
        spreadsheet_id,
        sheet_name,
        video,
        columns,
        row_number=row_number,
    )


def _write_video_to_named_sheet(
    service,
    spreadsheet_id: str,
    sheet_name: str,
    video: dict[str, Any],
    columns: list[str],
    *,
    row_number: int | None,
) -> int:
    end_column = _column_letter(len(columns))
    row_values = [video_to_row(video, columns)]
    if row_number:
        (
            service.spreadsheets()
            .values()
            .update(
                spreadsheetId=spreadsheet_id,
                range=_sheet_range(sheet_name, f"A{row_number}:{end_column}{row_number}"),
                valueInputOption="RAW",
                body={"values": row_values},
            )
            .execute()
        )
        return int(row_number)
    response = (
        service.spreadsheets()
        .values()
        .append(
            spreadsheetId=spreadsheet_id,
            range=_sheet_range(sheet_name, f"A:{end_column}"),
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": row_values},
        )
        .execute()
    )
    updated_range = response.get("updates", {}).get("updatedRange", "")
    match = re.search(r"!A(\d+):", updated_range)
    return int(match.group(1)) if match else 0


def _project_sheet_rows_by_id(
    service,
    spreadsheet_id: str,
    project_sheets: list[str],
    video_id: int,
) -> dict[str, int]:
    response = (
        service.spreadsheets()
        .values()
        .batchGet(
            spreadsheetId=spreadsheet_id,
            ranges=[_sheet_range(title, "A2:A") for title in project_sheets],
            majorDimension="ROWS",
        )
        .execute()
    )
    value_ranges = response.get("valueRanges", [])
    found: dict[str, int] = {}
    for sheet_index, title in enumerate(project_sheets):
        rows = value_ranges[sheet_index].get("values", []) if sheet_index < len(value_ranges) else []
        for row_index, row in enumerate(rows, start=2):
            if row and str(row[0]) == str(video_id):
                found[title] = row_index
                break
    return found


def _managed_month_sheet_titles(
    service,
    spreadsheet_id: str,
    videos: list[dict[str, Any]] | None = None,
) -> list[str]:
    properties = _sheet_properties(service, spreadsheet_id)
    titles = {
        title for title in properties if reconciliation.MONTH_RE.match(title)
    }
    titles.update(reconciliation.BASE_MONTHS)
    for video in videos or []:
        month = reconciliation.publish_month(video)
        if month:
            titles.add(month)
    return [*sorted(titles), reconciliation.NO_DATE_SHEET]


def _sync_video_project_sheet(
    service,
    spreadsheet_id: str,
    video: dict[str, Any],
    columns: list[str],
) -> None:
    project_sheets = list(PROJECT_SHEET_TITLES.values())
    _ensure_named_sheets(
        service,
        spreadsheet_id,
        {title: columns for title in project_sheets},
    )
    target_title = project_sheet_title(str(video.get("project_code") or ""))
    existing_rows = _project_sheet_rows_by_id(
        service,
        spreadsheet_id,
        project_sheets,
        int(video["id"]),
    )
    end_column = _column_letter(len(columns))
    clear_ranges = [
        _sheet_range(title, f"A{row_number}:{end_column}{row_number}")
        for title, row_number in existing_rows.items()
        if title != target_title
    ]
    if clear_ranges:
        (
            service.spreadsheets()
            .values()
            .batchClear(
                spreadsheetId=spreadsheet_id,
                body={"ranges": clear_ranges},
            )
            .execute()
        )
    if target_title:
        _write_video_to_named_sheet(
            service,
            spreadsheet_id,
            target_title,
            video,
            columns,
            row_number=existing_rows.get(target_title),
        )


def _sync_video_month_sheet(
    service,
    spreadsheet_id: str,
    video: dict[str, Any],
    columns: list[str],
) -> None:
    month_sheets = _managed_month_sheet_titles(service, spreadsheet_id, [video])
    _ensure_named_sheets(
        service,
        spreadsheet_id,
        {title: columns for title in month_sheets},
    )
    target_title = reconciliation.month_partition_sheet(video)
    existing_rows = _project_sheet_rows_by_id(
        service,
        spreadsheet_id,
        month_sheets,
        int(video["id"]),
    )
    end_column = _column_letter(len(columns))
    clear_ranges = [
        _sheet_range(title, f"A{row_number}:{end_column}{row_number}")
        for title, row_number in existing_rows.items()
        if title != target_title
    ]
    if clear_ranges:
        service.spreadsheets().values().batchClear(
            spreadsheetId=spreadsheet_id,
            body={"ranges": clear_ranges},
        ).execute()
    _write_video_to_named_sheet(
        service,
        spreadsheet_id,
        target_title,
        video,
        columns,
        row_number=existing_rows.get(target_title),
    )


def _remove_video_from_managed_sheets(
    service,
    spreadsheet_id: str,
    video_id: int,
    columns: list[str],
) -> None:
    partition_titles = [
        *PROJECT_SHEET_TITLES.values(),
        *_managed_month_sheet_titles(service, spreadsheet_id),
    ]
    _ensure_named_sheets(
        service,
        spreadsheet_id,
        {title: columns for title in partition_titles},
    )
    titles = [
        SHEET_NAME,
        *partition_titles,
    ]
    existing_rows = _project_sheet_rows_by_id(
        service,
        spreadsheet_id,
        list(dict.fromkeys(titles)),
        video_id,
    )
    if not existing_rows:
        return
    end_column = _column_letter(len(columns))
    service.spreadsheets().values().batchClear(
        spreadsheetId=spreadsheet_id,
        body={
            "ranges": [
                _sheet_range(title, f"A{row_number}:{end_column}{row_number}")
                for title, row_number in existing_rows.items()
            ]
        },
    ).execute()


def upsert_video(video: dict[str, Any], *, service=None) -> int:
    settings = get_settings()
    if not settings.google_sheets_spreadsheet_id:
        raise RuntimeError("GOOGLE_SHEETS_SPREADSHEET_ID is not configured")

    service = service or _service()
    spreadsheet_id = settings.google_sheets_spreadsheet_id
    columns = _ensure_video_sheet_columns(service, spreadsheet_id)
    if video.get("status") == "deleted":
        _remove_video_from_managed_sheets(
            service,
            spreadsheet_id,
            int(video["id"]),
            columns,
        )
        return 0
    end_column = _column_letter(len(columns))
    row_values = [video_to_row(video, columns)]
    row_number = video.get("sheet_row") or _find_row_by_id(service, spreadsheet_id, int(video["id"]))

    if row_number:
        (
            service.spreadsheets()
            .values()
            .update(
                spreadsheetId=spreadsheet_id,
                range=f"{SHEET_NAME}!A{row_number}:{end_column}{row_number}",
                valueInputOption="RAW",
                body={"values": row_values},
            )
            .execute()
        )
        _sync_video_project_sheet(service, spreadsheet_id, video, columns)
        _sync_video_month_sheet(service, spreadsheet_id, video, columns)
        return int(row_number)

    response = (
        service.spreadsheets()
        .values()
        .append(
            spreadsheetId=spreadsheet_id,
            range=f"{SHEET_NAME}!A:{end_column}",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": row_values},
        )
        .execute()
    )
    updated_range = response.get("updates", {}).get("updatedRange", "")
    match = re.search(r"!A(\d+):", updated_range)
    if match:
        row_number = int(match.group(1))
        _sync_video_project_sheet(service, spreadsheet_id, video, columns)
        _sync_video_month_sheet(service, spreadsheet_id, video, columns)
        return row_number
    found = _find_row_by_id(service, spreadsheet_id, int(video["id"]))
    _sync_video_project_sheet(service, spreadsheet_id, video, columns)
    _sync_video_month_sheet(service, spreadsheet_id, video, columns)
    return int(found or 0)


def batch_upsert_videos(
    videos: list[dict[str, Any]],
    *,
    service=None,
) -> dict[int, int]:
    if not videos:
        return {}
    if len(videos) > 10:
        raise ValueError("batch_upsert_videos accepts at most 10 videos")
    settings = get_settings()
    if not settings.google_sheets_spreadsheet_id:
        raise RuntimeError("GOOGLE_SHEETS_SPREADSHEET_ID is not configured")

    service = service or _service()
    spreadsheet_id = settings.google_sheets_spreadsheet_id
    columns = _ensure_video_sheet_columns(service, spreadsheet_id)
    project_sheets = list(PROJECT_SHEET_TITLES.values())
    month_sheets = _managed_month_sheet_titles(service, spreadsheet_id, videos)
    partition_sheets = [*project_sheets, *month_sheets]
    _ensure_named_sheets(
        service,
        spreadsheet_id,
        {title: columns for title in partition_sheets},
    )
    sheet_names = [SHEET_NAME, *partition_sheets]
    response = (
        service.spreadsheets()
        .values()
        .batchGet(
            spreadsheetId=spreadsheet_id,
            ranges=[_sheet_range(title, "A2:A") for title in sheet_names],
            majorDimension="ROWS",
        )
        .execute()
    )
    value_ranges = response.get("valueRanges", [])
    rows_by_sheet: dict[str, list[list[Any]]] = {}
    index_by_sheet: dict[str, dict[int, int]] = {}
    for index, title in enumerate(sheet_names):
        rows = value_ranges[index].get("values", []) if index < len(value_ranges) else []
        rows_by_sheet[title] = rows
        index_by_sheet[title] = {
            int(row[0]): row_number
            for row_number, row in enumerate(rows, start=2)
            if row and str(row[0]).isdigit()
        }

    next_row = {title: len(rows_by_sheet[title]) + 2 for title in sheet_names}
    end_column = _column_letter(len(columns))
    updates: list[dict[str, Any]] = []
    clear_ranges: list[str] = []
    main_rows: dict[int, int] = {}
    for video in videos:
        video_id = int(video["id"])
        row_values = [video_to_row(video, columns)]
        main_row = int(video.get("sheet_row") or index_by_sheet[SHEET_NAME].get(video_id) or 0)
        is_deleted = video.get("status") == "deleted"
        if is_deleted:
            if main_row:
                clear_ranges.append(
                    _sheet_range(SHEET_NAME, f"A{main_row}:{end_column}{main_row}")
                )
            main_rows[video_id] = 0
        else:
            if not main_row:
                main_row = next_row[SHEET_NAME]
                next_row[SHEET_NAME] += 1
            main_rows[video_id] = main_row
            updates.append(
                {
                    "range": _sheet_range(SHEET_NAME, f"A{main_row}:{end_column}{main_row}"),
                    "values": row_values,
                }
            )

        target_title = None if is_deleted else reconciliation.project_partition_sheet(video)
        for title in project_sheets:
            existing_row = index_by_sheet[title].get(video_id)
            if existing_row and title != target_title:
                clear_ranges.append(
                    _sheet_range(title, f"A{existing_row}:{end_column}{existing_row}")
                )
        if target_title:
            project_row = index_by_sheet[target_title].get(video_id)
            if not project_row:
                project_row = next_row[target_title]
                next_row[target_title] += 1
            updates.append(
                {
                    "range": _sheet_range(
                        target_title,
                        f"A{project_row}:{end_column}{project_row}",
                    ),
                    "values": row_values,
                }
            )

        target_month = None if is_deleted else reconciliation.month_partition_sheet(video)
        for title in month_sheets:
            existing_row = index_by_sheet[title].get(video_id)
            if existing_row and title != target_month:
                clear_ranges.append(
                    _sheet_range(title, f"A{existing_row}:{end_column}{existing_row}")
                )
        if target_month:
            month_row = index_by_sheet[target_month].get(video_id)
            if not month_row:
                month_row = next_row[target_month]
                next_row[target_month] += 1
            updates.append(
                {
                    "range": _sheet_range(
                        target_month,
                        f"A{month_row}:{end_column}{month_row}",
                    ),
                    "values": row_values,
                }
            )

    values_api = service.spreadsheets().values()
    if clear_ranges:
        values_api.batchClear(
            spreadsheetId=spreadsheet_id,
            body={"ranges": clear_ranges},
        ).execute()
    if updates:
        values_api.batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"valueInputOption": "RAW", "data": updates},
        ).execute()
    return main_rows


def _report_project(video: dict[str, Any]) -> tuple[str, str]:
    code = str(video.get("project_code") or "unassigned")
    if code == "other":
        return code, "Другие проекты"
    if code == "unassigned":
        return code, "Без проекта"
    project = next((item for item in REPORTING_PROJECTS if item["code"] == code), None)
    return code, str(video.get("project_name") or (project or {}).get("name") or code)


def _person_key(video: dict[str, Any], role: str) -> tuple[str, str] | None:
    name = str(video.get(f"{role}_name") or "").strip()
    if not name:
        return None
    username = str(video.get(f"{role}_username") or "").strip().lstrip("@")
    return name, username


def build_project_stats_rows(
    videos: list[dict[str, Any]],
    *,
    updated_at: datetime | None = None,
) -> list[list[str]]:
    return reconciliation.build_project_stats_rows(videos, updated_at=updated_at)


def build_people_projects_rows(videos: list[dict[str, Any]]) -> list[list[str]]:
    return reconciliation.build_people_projects_rows(videos)


def _replace_named_sheet(
    service,
    spreadsheet_id: str,
    sheet_name: str,
    columns: list[str],
    rows: list[list[str]],
) -> None:
    end_column = _column_letter(len(columns))
    (
        service.spreadsheets()
        .values()
        .clear(
            spreadsheetId=spreadsheet_id,
            range=_sheet_range(sheet_name, f"A:{end_column}"),
            body={},
        )
        .execute()
    )
    (
        service.spreadsheets()
        .values()
        .update(
            spreadsheetId=spreadsheet_id,
            range=_sheet_range(sheet_name, f"A1:{end_column}{len(rows) + 1}"),
            valueInputOption="RAW",
            body={"values": [columns, *rows]},
        )
        .execute()
    )


def sync_project_reports(videos: list[dict[str, Any]], *, service=None) -> None:
    settings = get_settings()
    if not settings.google_sheets_spreadsheet_id:
        raise RuntimeError("GOOGLE_SHEETS_SPREADSHEET_ID is not configured")
    service = service or _service()
    spreadsheet_id = settings.google_sheets_spreadsheet_id
    _ensure_named_sheets(
        service,
        spreadsheet_id,
        {
            PROJECT_STATS_SHEET_NAME: PROJECT_STATS_COLUMNS,
            MONTH_STATS_SHEET_NAME: MONTH_STATS_COLUMNS,
            PEOPLE_PROJECTS_SHEET_NAME: PEOPLE_PROJECTS_COLUMNS,
        },
    )
    _replace_named_sheet(
        service,
        spreadsheet_id,
        PROJECT_STATS_SHEET_NAME,
        PROJECT_STATS_COLUMNS,
        build_project_stats_rows(videos),
    )
    _replace_named_sheet(
        service,
        spreadsheet_id,
        MONTH_STATS_SHEET_NAME,
        MONTH_STATS_COLUMNS,
        reconciliation.build_month_stats_rows(videos),
    )
    _replace_named_sheet(
        service,
        spreadsheet_id,
        PEOPLE_PROJECTS_SHEET_NAME,
        PEOPLE_PROJECTS_COLUMNS,
        build_people_projects_rows(videos),
    )


def read_named_tables(
    sheet_names: list[str],
    *,
    service=None,
) -> dict[str, list[list[Any]]]:
    settings = get_settings()
    if not settings.google_sheets_spreadsheet_id:
        raise RuntimeError("GOOGLE_SHEETS_SPREADSHEET_ID is not configured")
    service = service or _service()
    spreadsheet_id = settings.google_sheets_spreadsheet_id
    properties = _sheet_properties(service, spreadsheet_id)
    unique_names = list(dict.fromkeys(sheet_names))
    existing = [name for name in unique_names if name in properties]
    result: dict[str, list[list[Any]]] = {name: [] for name in unique_names}
    if not existing:
        return result
    response = (
        service.spreadsheets()
        .values()
        .batchGet(
            spreadsheetId=spreadsheet_id,
            ranges=[_sheet_range(name, "A:AZ") for name in existing],
            majorDimension="ROWS",
        )
        .execute()
    )
    ranges = response.get("valueRanges", [])
    for index, name in enumerate(existing):
        result[name] = ranges[index].get("values", []) if index < len(ranges) else []
    return result


def read_reconciliation_tables(
    videos: list[dict[str, Any]],
    *,
    service=None,
) -> dict[str, list[list[Any]]]:
    settings = get_settings()
    if not settings.google_sheets_spreadsheet_id:
        raise RuntimeError("GOOGLE_SHEETS_SPREADSHEET_ID is not configured")
    service = service or _service()
    spreadsheet_id = settings.google_sheets_spreadsheet_id
    properties = _sheet_properties(service, spreadsheet_id)
    months = set(reconciliation.BASE_MONTHS)
    months.update(title for title in properties if reconciliation.MONTH_RE.match(title))
    months.update(
        month for video in videos if (month := reconciliation.publish_month(video))
    )
    names = [
        SHEET_NAME,
        *PROJECT_SHEET_TITLES.values(),
        *sorted(months),
        reconciliation.NO_DATE_SHEET,
        PROJECT_STATS_SHEET_NAME,
        MONTH_STATS_SHEET_NAME,
        PEOPLE_PROJECTS_SHEET_NAME,
    ]
    return read_named_tables(names, service=service)


def build_managed_sheet_specs(
    videos: list[dict[str, Any]],
    sessions: list[dict[str, Any]],
    backfill_items: list[dict[str, Any]],
    reconciliation_result_rows: list[list[str]],
) -> list[dict[str, Any]]:
    active = sorted(reconciliation.active_videos(videos), key=reconciliation.canonical_sort_key)
    specs: list[dict[str, Any]] = [
        {
            "name": SHEET_NAME,
            "columns": SHEET_COLUMNS,
            "rows": [video_to_row(video, SHEET_COLUMNS) for video in active],
        }
    ]
    for title in PROJECT_SHEET_TITLES.values():
        rows = [
            video_to_row(video, SHEET_COLUMNS)
            for video in active
            if reconciliation.project_partition_sheet(video) == title
        ]
        specs.append({"name": title, "columns": SHEET_COLUMNS, "rows": rows})
    months = set(reconciliation.BASE_MONTHS)
    months.update(
        month for video in active if (month := reconciliation.publish_month(video))
    )
    for title in [*sorted(months), reconciliation.NO_DATE_SHEET]:
        rows = [
            video_to_row(video, SHEET_COLUMNS)
            for video in active
            if reconciliation.month_partition_sheet(video) == title
        ]
        specs.append({"name": title, "columns": SHEET_COLUMNS, "rows": rows})
    specs.extend(
        [
            {
                "name": PROJECT_STATS_SHEET_NAME,
                "columns": PROJECT_STATS_COLUMNS,
                "rows": reconciliation.build_project_stats_rows(active),
            },
            {
                "name": MONTH_STATS_SHEET_NAME,
                "columns": MONTH_STATS_COLUMNS,
                "rows": reconciliation.build_month_stats_rows(active),
            },
            {
                "name": PEOPLE_PROJECTS_SHEET_NAME,
                "columns": PEOPLE_PROJECTS_COLUMNS,
                "rows": reconciliation.build_people_projects_rows(active),
            },
            {
                "name": UNFINISHED_SHEET_NAME,
                "columns": reconciliation.UNFINISHED_COLUMNS,
                "rows": reconciliation.build_unfinished_rows(active),
            },
            {
                "name": UNSUBMITTED_SHEET_NAME,
                "columns": reconciliation.UNSUBMITTED_COLUMNS,
                "rows": reconciliation.build_unsubmitted_rows(sessions),
            },
            {
                "name": RECONCILIATION_SHEET_NAME,
                "columns": reconciliation.RECONCILIATION_COLUMNS,
                "rows": reconciliation_result_rows,
            },
            {
                "name": BACKFILL_REVIEW_SHEET_NAME,
                "columns": reconciliation.BACKFILL_REVIEW_COLUMNS,
                "rows": [reconciliation.backfill_review_row(item) for item in backfill_items],
            },
        ]
    )
    return specs


def write_staging_sheet(
    sheet_name: str,
    columns: list[str],
    rows: list[list[str]],
    *,
    service=None,
) -> None:
    settings = get_settings()
    if not settings.google_sheets_spreadsheet_id:
        raise RuntimeError("GOOGLE_SHEETS_SPREADSHEET_ID is not configured")
    service = service or _service()
    spreadsheet_id = settings.google_sheets_spreadsheet_id
    _ensure_named_sheets(service, spreadsheet_id, {sheet_name: columns})
    _replace_named_sheet(service, spreadsheet_id, sheet_name, columns, rows)


def promote_staging_sheets(
    staging_map: dict[str, str],
    *,
    run_id: int,
    service=None,
) -> None:
    settings = get_settings()
    if not settings.google_sheets_spreadsheet_id:
        raise RuntimeError("GOOGLE_SHEETS_SPREADSHEET_ID is not configured")
    service = service or _service()
    spreadsheet_id = settings.google_sheets_spreadsheet_id
    properties = _sheet_properties(service, spreadsheet_id)
    requests: list[dict[str, Any]] = []
    old_sheet_ids: list[int] = []
    for index, (final_name, staging_name) in enumerate(staging_map.items()):
        staging = properties.get(staging_name)
        if not staging:
            raise RuntimeError(f"staging sheet is missing: {staging_name}")
        old_name = f"__old__r{run_id}_{index:02d}"
        lingering_old = properties.get(old_name)
        if lingering_old:
            requests.append({"deleteSheet": {"sheetId": int(lingering_old["sheetId"])}})
        current = properties.get(final_name)
        if current:
            requests.append(
                {
                    "updateSheetProperties": {
                        "properties": {"sheetId": int(current["sheetId"]), "title": old_name},
                        "fields": "title",
                    }
                }
            )
            old_sheet_ids.append(int(current["sheetId"]))
        requests.append(
            {
                "updateSheetProperties": {
                    "properties": {
                        "sheetId": int(staging["sheetId"]),
                        "title": final_name,
                    },
                    "fields": "title",
                }
            }
        )
    requests.extend({"deleteSheet": {"sheetId": sheet_id}} for sheet_id in old_sheet_ids)
    if requests:
        (
            service.spreadsheets()
            .batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={"requests": requests},
            )
            .execute()
        )


def replace_reconciliation_result(
    rows: list[list[str]],
    *,
    service=None,
) -> None:
    settings = get_settings()
    if not settings.google_sheets_spreadsheet_id:
        raise RuntimeError("GOOGLE_SHEETS_SPREADSHEET_ID is not configured")
    service = service or _service()
    spreadsheet_id = settings.google_sheets_spreadsheet_id
    _ensure_named_sheets(
        service,
        spreadsheet_id,
        {RECONCILIATION_SHEET_NAME: reconciliation.RECONCILIATION_COLUMNS},
    )
    _replace_named_sheet(
        service,
        spreadsheet_id,
        RECONCILIATION_SHEET_NAME,
        reconciliation.RECONCILIATION_COLUMNS,
        rows,
    )


def sync_unfinished_reports(
    videos: list[dict[str, Any]],
    sessions: list[dict[str, Any]],
    *,
    service=None,
) -> None:
    settings = get_settings()
    if not settings.google_sheets_spreadsheet_id:
        raise RuntimeError("GOOGLE_SHEETS_SPREADSHEET_ID is not configured")
    service = service or _service()
    spreadsheet_id = settings.google_sheets_spreadsheet_id
    _ensure_named_sheets(
        service,
        spreadsheet_id,
        {
            UNFINISHED_SHEET_NAME: reconciliation.UNFINISHED_COLUMNS,
            UNSUBMITTED_SHEET_NAME: reconciliation.UNSUBMITTED_COLUMNS,
        },
    )
    _replace_named_sheet(
        service,
        spreadsheet_id,
        UNFINISHED_SHEET_NAME,
        reconciliation.UNFINISHED_COLUMNS,
        reconciliation.build_unfinished_rows(videos),
    )
    _replace_named_sheet(
        service,
        spreadsheet_id,
        UNSUBMITTED_SHEET_NAME,
        reconciliation.UNSUBMITTED_COLUMNS,
        reconciliation.build_unsubmitted_rows(sessions),
    )


def _sheet_titles(service, spreadsheet_id: str) -> set[str]:
    return set(_sheet_properties(service, spreadsheet_id))


def _ensure_metrics_sheet(service, spreadsheet_id: str) -> None:
    if METRICS_SHEET_NAME not in _sheet_titles(service, spreadsheet_id):
        (
            service.spreadsheets()
            .batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={"requests": [{"addSheet": {"properties": {"title": METRICS_SHEET_NAME}}}]},
            )
            .execute()
        )
    (
        service.spreadsheets()
        .values()
        .update(
            spreadsheetId=spreadsheet_id,
            range=f"{METRICS_SHEET_NAME}!A1:O1",
            valueInputOption="USER_ENTERED",
            body={"values": [METRICS_COLUMNS]},
        )
        .execute()
    )


def _metric_date_key(value: Any) -> str:
    if hasattr(value, "date"):
        return value.date().isoformat()
    text = str(value or "")
    return text[:10]


def _existing_metric_keys(service, spreadsheet_id: str) -> set[tuple[str, str, str]]:
    result = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=f"{METRICS_SHEET_NAME}!A2:C")
        .execute()
    )
    keys: set[tuple[str, str, str]] = set()
    for row in result.get("values", []):
        if len(row) >= 3:
            keys.add((str(row[1]), str(row[2]), _metric_date_key(row[0])))
    return keys


def metric_snapshot_to_row(snapshot: dict[str, Any]) -> list[str]:
    values = {
        "captured_at": snapshot.get("captured_at"),
        "video_id": snapshot.get("video_id"),
        "platform": snapshot.get("platform"),
        "platform_video_id": snapshot.get("platform_video_id"),
        "views": snapshot.get("views"),
        "likes": snapshot.get("likes"),
        "comments": snapshot.get("comments"),
        "shares": snapshot.get("shares"),
        "source_status": snapshot.get("source_status"),
        "error_message": snapshot.get("error_message"),
        "instagram_url": snapshot.get("instagram_url"),
        "youtube_url": snapshot.get("youtube_url"),
        "author": person_value(snapshot, "author"),
        "montage": person_value(snapshot, "montage"),
        "voice": person_value(snapshot, "voice") if snapshot.get("voice_name") else "",
    }
    return [_as_cell(values[column]) for column in METRICS_COLUMNS]


def append_metric_snapshots(snapshots: list[dict[str, Any]]) -> int:
    settings = get_settings()
    if not settings.google_sheets_spreadsheet_id:
        raise RuntimeError("GOOGLE_SHEETS_SPREADSHEET_ID is not configured")

    ok_snapshots = [snapshot for snapshot in snapshots if snapshot.get("source_status") == "ok"]
    if not ok_snapshots:
        return 0

    service = _service()
    spreadsheet_id = settings.google_sheets_spreadsheet_id
    _ensure_metrics_sheet(service, spreadsheet_id)
    existing = _existing_metric_keys(service, spreadsheet_id)

    rows: list[list[str]] = []
    for snapshot in ok_snapshots:
        key = (
            str(snapshot.get("video_id")),
            str(snapshot.get("platform")),
            _metric_date_key(snapshot.get("captured_at")),
        )
        if key in existing:
            continue
        existing.add(key)
        rows.append(metric_snapshot_to_row(snapshot))

    if not rows:
        return 0

    (
        service.spreadsheets()
        .values()
        .append(
            spreadsheetId=spreadsheet_id,
            range=f"{METRICS_SHEET_NAME}!A:O",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": rows},
        )
        .execute()
    )
    return len(rows)
