from __future__ import annotations

from typing import Any

from bot import db
from bot.telegram import TelegramClient


COMMAND = "/cancel"


def is_cancel_message(message: dict[str, Any]) -> bool:
    text = str(message.get("text") or "").strip()
    if not text.startswith("/"):
        return False
    command = text.split(maxsplit=1)[0].split("@", 1)[0].lower()
    return command == COMMAND


def handle_message(message: dict[str, Any]) -> bool:
    if not is_cancel_message(message):
        return False

    from bot import handlers as h

    actor = h._actor_from_message(message)
    if not actor:
        return True

    db.clear_session(actor.tg_id)
    tg = TelegramClient()
    h._send_main_menu(
        tg,
        actor,
        "❌ Текущий сценарий отменён. Незавершённые данные не сохранены.",
    )
    return True
