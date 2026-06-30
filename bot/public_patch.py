from __future__ import annotations

import random
from typing import Any

from bot import db
from bot import handlers as h
from bot.config import get_settings
from bot.telegram import TelegramClient, inline_keyboard

TARGET_USER_ID = 338354945
HEARING_MODE_KEY = f"hearing_mode:{TARGET_USER_ID}"
HEARING_REPLIES = (
    "А?",
    "Что?",
    "М?",
    "Чего?",
    "Не расслышал",
    "Повтори",
    "Громче",
    "Кто здесь?",
    "Я плохо слышу",
    "Шумит что-то",
    "Можно ещё раз?",
    "Связь пропадает",
    "Ты что-то сказала?",
    "Алло?",
    "Приём?",
)

_ORIGINAL_HANDLE_MESSAGE = h.handle_message
_ORIGINAL_HANDLE_CALLBACK = h.handle_callback
_ORIGINAL_IS_ADMIN = h.is_admin
_ORIGINAL_SEND_MAIN_MENU = h._send_main_menu


def _ensure_fun_flags_table() -> None:
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS bot_fun_flags (
            key text PRIMARY KEY,
            value text NOT NULL,
            updated_by_tg_id bigint,
            updated_by_username text,
            updated_at timestamptz DEFAULT now()
        )
        """
    )


def _hearing_mode_enabled() -> bool:
    try:
        _ensure_fun_flags_table()
        row = db.fetch_one("SELECT value FROM bot_fun_flags WHERE key = %s", (HEARING_MODE_KEY,))
        return bool(row and row.get("value") == "on")
    except Exception:
        return False


def _set_hearing_mode(enabled: bool, actor: h.Actor) -> None:
    _ensure_fun_flags_table()
    db.execute(
        """
        INSERT INTO bot_fun_flags (key, value, updated_by_tg_id, updated_by_username, updated_at)
        VALUES (%s, %s, %s, %s, now())
        ON CONFLICT (key)
        DO UPDATE SET
            value = EXCLUDED.value,
            updated_by_tg_id = EXCLUDED.updated_by_tg_id,
            updated_by_username = EXCLUDED.updated_by_username,
            updated_at = now()
        """,
        (HEARING_MODE_KEY, "on" if enabled else "off", actor.tg_id, actor.username),
    )
    h.record_system_log(
        "hearing_mode_enabled" if enabled else "hearing_mode_disabled",
        "bot_fun_flag",
        None,
        {"target_user_id": TARGET_USER_ID, "enabled": enabled},
        actor,
    )


def is_admin(tg_id: int) -> bool:
    return tg_id == TARGET_USER_ID or _ORIGINAL_IS_ADMIN(tg_id)


def _send_main_menu(tg: TelegramClient, actor: h.Actor, text: str) -> None:
    rows = [
        [("Новое видео", "cmd:new"), ("Мои заявки", "cmd:my")],
        [("Помощь", "cmd:help")],
    ]
    if h.is_admin(actor.tg_id):
        rows.insert(1, [("Админка", "cmd:admin"), ("Сводка", "cmd:summary")])
        rows.insert(2, [("Переотправить pending", "cmd:resend_pending")])
    if h.is_superadmin(actor.tg_id):
        status = "вкл" if _hearing_mode_enabled() else "выкл"
        rows.append([(f"👂 Режим «А?» сейчас: {status}", "fun:hearing:status")])
        rows.append([("Включить режим «А?»", "fun:hearing:on"), ("Выключить режим «А?»", "fun:hearing:off")])
    tg.send_message(actor.chat_id, text, inline_keyboard(rows))


def _reply_like_bad_hearing(tg: TelegramClient, message: dict[str, Any], actor: h.Actor) -> None:
    payload = {
        "chat_id": actor.chat_id,
        "text": random.choice(HEARING_REPLIES),
        "reply_to_message_id": message.get("message_id"),
        "allow_sending_without_reply": True,
    }
    try:
        tg._request("sendMessage", payload)
    except Exception:
        tg.send_message(actor.chat_id, payload["text"])


def _toggle_hearing_mode(tg: TelegramClient, actor: h.Actor, enabled: bool | None = None) -> None:
    if not h.require_superadmin(tg, actor):
        return
    if enabled is None:
        state = "включён" if _hearing_mode_enabled() else "выключен"
        tg.send_message(actor.chat_id, f"Режим «А?» для {TARGET_USER_ID}: {state}.")
        return
    _set_hearing_mode(enabled, actor)
    tg.send_message(
        actor.chat_id,
        f"Режим «А?» {'включён' if enabled else 'выключен'} для пользователя {TARGET_USER_ID}.",
    )


def handle_message(message: dict[str, Any]) -> None:
    actor = h._actor_from_message(message)
    if not actor:
        return
    text = (message.get("text") or "").strip()
    tg = TelegramClient()

    if (
        actor.tg_id == TARGET_USER_ID
        and actor.chat_type in {"group", "supergroup"}
        and text
        and not text.startswith("/")
        and _hearing_mode_enabled()
    ):
        _reply_like_bad_hearing(tg, message, actor)
        return

    if text.startswith("/"):
        command, rest = h._command_parts(text)
        if command == "/start" and rest.lower() in {"submit", "new_video", "new"}:
            h.db.clear_session(actor.tg_id)
            h.start_new_video(tg, actor)
            return
        if command == "/hearing_on":
            _toggle_hearing_mode(tg, actor, True)
            return
        if command == "/hearing_off":
            _toggle_hearing_mode(tg, actor, False)
            return
        if command == "/hearing_status":
            _toggle_hearing_mode(tg, actor, None)
            return

    _ORIGINAL_HANDLE_MESSAGE(message)


def handle_callback(callback: dict[str, Any]) -> None:
    actor = h._actor_from_callback(callback)
    if not actor:
        return
    data = callback.get("data") or ""
    if data.startswith("fun:hearing:"):
        tg = TelegramClient()
        try:
            tg.answer_callback_query(callback["id"])
        except Exception:
            pass
        action = data.rsplit(":", 1)[-1]
        if action == "on":
            _toggle_hearing_mode(tg, actor, True)
        elif action == "off":
            _toggle_hearing_mode(tg, actor, False)
        else:
            _toggle_hearing_mode(tg, actor, None)
        return
    _ORIGINAL_HANDLE_CALLBACK(callback)


h.is_admin = is_admin
h._send_main_menu = _send_main_menu
h.handle_message = handle_message
h.handle_callback = handle_callback

handle_update = h.handle_update
record_system_log = h.record_system_log
