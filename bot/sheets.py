from __future__ import annotations

import base64
import json
import re
from typing import Any

from google.oauth2 import service_account
from googleapiclient.discovery import build

from bot.config import get_settings


SHEET_NAME = "Videos"
SHEET_COLUMNS = [
    "id",
    "status",
    "publish_date",
    "instagram_url",
    "instagram_id",
    "youtube_url",
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


def video_to_row(video: dict[str, Any]) -> list[str]:
    values = {
        "id": video.get("id"),
        "status": video.get("status"),
        "publish_date": video.get("publish_date"),
        "instagram_url": video.get("instagram_url"),
        "instagram_id": video.get("instagram_id"),
        "youtube_url": video.get("youtube_url"),
        "tiktok_url": video.get("tiktok_url"),
        "vk_url": video.get("vk_url"),
        "author": video.get("author_name"),
        "author_tg_id": video.get("author_tg_id"),
        "montage": video.get("montage_name"),
        "montage_tg_id": video.get("montage_tg_id"),
        "voice": video.get("voice_name"),
        "voice_tg_id": video.get("voice_tg_id"),
        "added_by": _user_cell(video.get("added_by_username"), video.get("added_by_tg_id")),
        "checked_by": _user_cell(video.get("checked_by_username"), video.get("checked_by_tg_id")),
        "created_at": video.get("created_at"),
        "checked_at": video.get("checked_at"),
        "batch_id": video.get("batch_id"),
        "comment": video.get("comment"),
    }
    return [_as_cell(values[column]) for column in SHEET_COLUMNS]


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
    row_values = [video_to_row(video)]
    row_number = video.get("sheet_row") or _find_row_by_id(service, spreadsheet_id, int(video["id"]))

    if row_number:
        (
            service.spreadsheets()
            .values()
            .update(
                spreadsheetId=spreadsheet_id,
                range=f"{SHEET_NAME}!A{row_number}:T{row_number}",
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
            range=f"{SHEET_NAME}!A:T",
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

