from __future__ import annotations

import os


REQUIRED_GROUPS = {
    "BOT_TOKEN": ("BOT_TOKEN", "TELEGRAM_BOT_TOKEN"),
    "BOT_USERNAME": ("BOT_USERNAME", "TELEGRAM_BOT_USERNAME"),
    "WEBHOOK_SECRET": ("WEBHOOK_SECRET", "TELEGRAM_WEBHOOK_SECRET"),
    "DATABASE_URL": ("DATABASE_URL",),
    "GOOGLE_SHEETS_SPREADSHEET_ID": ("GOOGLE_SHEETS_SPREADSHEET_ID",),
    "GOOGLE_SERVICE_ACCOUNT_JSON_B64": ("GOOGLE_SERVICE_ACCOUNT_JSON_B64",),
    "WORK_CHAT_ID": ("WORK_CHAT_ID",),
    "ADMIN_CHAT_ID": ("ADMIN_CHAT_ID",),
    "TZ": ("TZ", "TIMEZONE", "DEFAULT_TIMEZONE"),
}


def has_any(names: tuple[str, ...]) -> bool:
    return any(bool(os.environ.get(name)) for name in names)


def main() -> int:
    missing = [label for label, names in REQUIRED_GROUPS.items() if not has_any(names)]
    if missing:
        print("Missing env groups:")
        for name in missing:
            print(f"- {name}")
        return 1
    print("All required env groups are present.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

