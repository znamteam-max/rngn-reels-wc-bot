from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from zoneinfo import ZoneInfo


DEFAULT_ADMIN_CHAT_ID = -5520370963
DEFAULT_TIMEZONE = "Europe/Helsinki"


def _env(name: str, *aliases: str, default: str | None = None) -> str | None:
    for key in (name, *aliases):
        value = os.environ.get(key)
        if value:
            return value.strip()
    return default


def _int_env(name: str, default: int) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _int_list_env(name: str, *aliases: str) -> set[int]:
    raw = _env(name, *aliases, default="") or ""
    values: set[int] = set()
    for item in raw.replace(";", ",").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            values.add(int(item))
        except ValueError:
            continue
    return values


@dataclass(frozen=True)
class Settings:
    database_url: str | None
    bot_token: str | None
    bot_username: str | None
    webhook_secret: str | None
    google_service_account_json_b64: str | None
    google_sheets_spreadsheet_id: str | None
    admin_chat_id: int
    timezone: str
    bootstrap_superadmin_ids: set[int]
    batch_window_minutes: int = 15

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings(
        database_url=_env("DATABASE_URL"),
        bot_token=_env("BOT_TOKEN", "TELEGRAM_BOT_TOKEN"),
        bot_username=_env("BOT_USERNAME", "TELEGRAM_BOT_USERNAME"),
        webhook_secret=_env("WEBHOOK_SECRET", "TELEGRAM_WEBHOOK_SECRET"),
        google_service_account_json_b64=_env("GOOGLE_SERVICE_ACCOUNT_JSON_B64"),
        google_sheets_spreadsheet_id=_env("GOOGLE_SHEETS_SPREADSHEET_ID"),
        admin_chat_id=_int_env("ADMIN_CHAT_ID", DEFAULT_ADMIN_CHAT_ID),
        timezone=_env("TIMEZONE", "TZ", "DEFAULT_TIMEZONE", default=DEFAULT_TIMEZONE) or DEFAULT_TIMEZONE,
        bootstrap_superadmin_ids=_int_list_env("BOOTSTRAP_SUPERADMIN_IDS", "ALLOWED_TELEGRAM_USER_IDS"),
    )


def missing_env_names() -> list[str]:
    settings = get_settings()
    missing: list[str] = []
    if not settings.database_url:
        missing.append("DATABASE_URL")
    if not settings.bot_token:
        missing.append("BOT_TOKEN")
    if not settings.webhook_secret:
        missing.append("WEBHOOK_SECRET")
    if not settings.google_service_account_json_b64:
        missing.append("GOOGLE_SERVICE_ACCOUNT_JSON_B64")
    if not settings.google_sheets_spreadsheet_id:
        missing.append("GOOGLE_SHEETS_SPREADSHEET_ID")
    return missing
