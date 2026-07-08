from __future__ import annotations

import base64
import json
import re
from typing import Any

from google.oauth2 import service_account
from googleapiclient.discovery import build

from bot.config import get_settings
from bot.messages import person_value


SHEET_NAME = "Videos"
METRICS_SHEET_NAME = "MetricsRaw"
SHEET_COLUMNS = [
    "id",
    "status",
    "video_type",
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
]
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


def _find_row_by_id(service, spreadsheet_id: str, video_id: int) -> int | None:
    result = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=f"{SHEET_NAME}!A2:A")
        .execute()
    )
    for index, row in enumerate(result.get("values", []), start=2):
        if row and str(row[0]) == str(video_id):
            return index
    return None


def upsert_video(video: dict[str, Any]) -> int:
    settings = get_settings()
    if not settings.google_sheets_spreadsheet_id:
        raise RuntimeError("GOOGLE_SHEETS_SPREADSHEET_ID is not configured")

    service = _service()
    spreadsheet_id = settings.google_sheets_spreadsheet_id
    columns = _ensure_video_sheet_columns(service, spreadsheet_id)
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
                valueInputOption="USER_ENTERED",
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
            range=f"{SHEET_NAME}!A:{end_column}",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": row_values},
        )
        .execute()
    )
    updated_range = response.get("updates", {}).get("updatedRange", "")
    match = re.search(r"!A(\d+):", updated_range)
    if match:
        return int(match.group(1))
    found = _find_row_by_id(service, spreadsheet_id, int(video["id"]))
    return int(found or 0)


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
