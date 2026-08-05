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
    ("queue_status", "Статус очереди"),
    ("find", "Найти заявку"),
    ("person", "Карточка участника"),
    ("daily_report", "Ежедневный отчёт"),
    ("chatid", "Показать ID текущего чата"),
    ("resend_pending", "Восстановить текущую FIFO-карточку"),
    ("return_missing_dates", "Вернуть заявки без даты"),
    ("jobs_status", "Статус фоновых заданий"),
    ("test_admin_chat", "Проверить админский чат"),
    ("sync_youtube_metrics", "Обновить YouTube-метрики"),
    ("metrics_youtube_today", "YouTube сегодня"),
    ("metrics_youtube_all", "YouTube всего"),
    ("metrics_video", "Метрики одного видео"),
]
SUPERADMIN_COMMANDS = [
    *COMMANDS,
    ("add_znambo", "Быстро добавить мой ролик"),
    ("reset_admin_queue", "Сбросить и восстановить FIFO-очередь"),
    ("retry_failed_jobs", "Повторить временно упавшие задания"),
]


def parse_superadmin_ids(value: str | None) -> list[int]:
    ids: list[int] = []
    for item in (value or "").replace(";", ",").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            ids.append(int(item))
        except ValueError:
            print(f"Skipping invalid BOOTSTRAP_SUPERADMIN_IDS item: {item}", file=sys.stderr)
    return ids


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
    for superadmin_id in parse_superadmin_ids(os.environ.get("BOOTSTRAP_SUPERADMIN_IDS")):
        telegram_post(
            token,
            "setMyCommands",
            {
                "scope": {"type": "chat", "chat_id": superadmin_id},
                "commands": [
                    {"command": command, "description": description}
                    for command, description in SUPERADMIN_COMMANDS
                ],
            },
        )
    telegram_post(token, "setChatMenuButton", {"menu_button": {"type": "commands"}})

    webhook_base_url = (
        os.environ.get("WEBHOOK_URL")
        or os.environ.get("APP_BASE_URL")
        or os.environ.get("VERCEL_PROJECT_PRODUCTION_URL")
        or ""
    ).strip().rstrip("/")
    webhook_secret = (os.environ.get("WEBHOOK_SECRET") or "").strip()
    if webhook_base_url and webhook_secret:
        if not webhook_base_url.startswith(("http://", "https://")):
            webhook_base_url = f"https://{webhook_base_url}"
        try:
            max_connections = int(os.environ.get("TELEGRAM_MAX_CONNECTIONS", "5"))
        except ValueError:
            max_connections = 5
        telegram_post(
            token,
            "setWebhook",
            {
                "url": f"{webhook_base_url}/api/webhook",
                "secret_token": webhook_secret,
                "max_connections": max(1, min(100, max_connections)),
                "allowed_updates": ["message", "callback_query"],
            },
        )
        print(f"Telegram webhook configured with max_connections={max_connections}.")
    else:
        print("Webhook unchanged: WEBHOOK_URL/APP_BASE_URL or WEBHOOK_SECRET is missing.")

    print("Telegram bot commands and menu button are configured.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
