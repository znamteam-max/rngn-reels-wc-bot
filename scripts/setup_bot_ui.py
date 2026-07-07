from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import requests


COMMANDS = [
    ("start", "Главное меню"),
    ("new_video", "Добавить Reels"),
    ("new_bigrecap", "Добавить большой рекап"),
    ("my_requests", "Мои заявки"),
    ("help", "Помощь"),
    ("admin", "Админская очередь"),
    ("chatid", "Показать ID текущего чата"),
    ("resend_pending", "Переотправить pending в админский чат"),
    ("test_admin_chat", "Проверить админский чат"),
    ("sync_youtube_metrics", "Обновить YouTube-метрики"),
    ("metrics_youtube_today", "YouTube сегодня"),
    ("metrics_youtube_all", "YouTube всего"),
    ("metrics_video", "Метрики одного видео"),
]


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def telegram_post(token: str, method: str, payload: dict[str, Any]) -> dict[str, Any]:
    response = requests.post(
        f"https://api.telegram.org/bot{token}/{method}",
        json=payload,
        timeout=15,
    )
    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError(f"{method}: Telegram returned non-JSON response") from exc
    if not data.get("ok"):
        description = data.get("description", "unknown Telegram API error")
        raise RuntimeError(f"{method}: {description}")
    return data


def main() -> int:
    load_env_file(Path(".env"))
    load_env_file(Path(".vercel/.env.production.local"))

    token = os.environ.get("BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        print("BOT_TOKEN is not configured.", file=sys.stderr)
        return 1

    telegram_post(
        token,
        "setMyCommands",
        {
            "commands": [
                {"command": command, "description": description}
                for command, description in COMMANDS
            ],
        },
    )
    telegram_post(token, "setChatMenuButton", {"menu_button": {"type": "commands"}})

    print("Telegram bot commands and menu button are configured.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
