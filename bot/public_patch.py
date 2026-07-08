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
_ORIGINAL_INSERT_PENDING_VIDEO = h.insert_pending_video


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
        [("➕ Добавить ролик", "cmd:new")],
        [("🧵 Добавить большой рекап", "cmd:new_bigrecap")],
        [("📋 Мои заявки", "cmd:my"), ("ℹ️ Помощь", "cmd:help")],
    ]
    if h.is_admin(actor.tg_id):
        rows.insert(3, [("Админка", "cmd:admin"), ("Сводка", "cmd:summary")])
        rows.insert(4, [("Переотправить pending", "cmd:resend_pending"), ("Тест админ-чата", "cmd:test_admin_chat")])
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


def _test_admin_chat(tg: TelegramClient, actor: h.Actor) -> None:
    if not h.require_admin(tg, actor):
        return
    settings = get_settings()
    try:
        response = tg.send_message(
            settings.admin_chat_id,
            f"Тест админского чата от @{actor.username}" if actor.username else f"Тест админского чата от {actor.tg_id}",
        )
        message_id = h._message_id(response)
        tg.send_message(
            actor.chat_id,
            "ADMIN_CHAT_ID работает.\n"
            f"chat_id: {settings.admin_chat_id}\n"
            f"message_id: {message_id if message_id is not None else 'unknown'}",
        )
    except Exception as exc:
        payload = h.telegram_failure_payload(exc, int(settings.admin_chat_id), "test_admin_chat")
        h.record_system_log("admin_chat_test_failed", "telegram_chat", None, payload, actor)
        tg.send_message(
            actor.chat_id,
            "ADMIN_CHAT_ID не работает.\n"
            f"chat_id: {settings.admin_chat_id}\n"
            f"Telegram error: {payload.get('telegram_status_code', '?')} "
            f"{payload.get('telegram_description') or payload.get('error')}",
        )


def insert_pending_video(actor: h.Actor, data: dict[str, Any]) -> dict[str, Any]:
    data = h.normalized_submission_data(data)
    video_type = h.normalize_video_type(data.get("video_type"))
    instagram_id = data.get("instagram_id")
    youtube_id = data.get("youtube_id")
    if video_type == h.VIDEO_TYPE_BIGRECAP:
        duplicate_column = "youtube_id"
        duplicate_value = youtube_id
    else:
        duplicate_column = "instagram_id"
        duplicate_value = instagram_id
    if not duplicate_value:
        return _ORIGINAL_INSERT_PENDING_VIDEO(actor, data)

    with db.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT id, batch_id
                FROM videos
                WHERE {duplicate_column} = %s
                  AND status = 'deleted'
                ORDER BY updated_at DESC, id DESC
                LIMIT 1
                """,
                (duplicate_value,),
            )
            deleted = cur.fetchone()
            if not deleted:
                return _ORIGINAL_INSERT_PENDING_VIDEO(actor, data)

            old_batch_id = deleted.get("batch_id")
            batch_id = h.ensure_open_batch(conn, actor)
            cur.execute(
                """
                UPDATE videos
                SET status = 'pending',
                    video_type = %s,
                    publish_date = %s,
                    instagram_url = %s,
                    instagram_id = %s,
                    youtube_url = %s,
                    youtube_id = %s,
                    tiktok_url = %s,
                    tiktok_id = %s,
                    vk_url = %s,
                    vk_id = %s,
                    author_id = %s,
                    author_name = %s,
                    author_username = %s,
                    montage_id = %s,
                    montage_name = %s,
                    montage_username = %s,
                    montage_same_as_author = %s,
                    voice_id = %s,
                    voice_name = %s,
                    voice_username = %s,
                    added_by_tg_id = %s,
                    added_by_username = %s,
                    checked_by_tg_id = NULL,
                    checked_by_username = NULL,
                    checked_at = NULL,
                    publish_date_set_by_tg_id = NULL,
                    publish_date_set_by_username = NULL,
                    publish_date_set_at = NULL,
                    admin_message_chat_id = NULL,
                    admin_message_id = NULL,
                    admin_notified_at = NULL,
                    batch_id = %s,
                    comment = NULL,
                    updated_at = now()
                WHERE id = %s
                RETURNING id
                """,
                (
                    h.normalize_video_type(data.get("video_type")),
                    data.get("publish_date"),
                    data.get("instagram_url"),
                    data.get("instagram_id"),
                    data.get("youtube_url"),
                    data.get("youtube_id"),
                    data.get("tiktok_url"),
                    data.get("tiktok_id"),
                    data.get("vk_url"),
                    data.get("vk_id"),
                    data.get("author_id"),
                    data.get("author_name"),
                    data.get("author_username"),
                    data.get("montage_id"),
                    data.get("montage_name"),
                    data.get("montage_username"),
                    bool(data.get("montage_same_as_author")),
                    data.get("voice_id"),
                    data.get("voice_name"),
                    data.get("voice_username"),
                    actor.tg_id,
                    actor.username,
                    batch_id,
                    deleted["id"],
                ),
            )

        if old_batch_id and int(old_batch_id) != int(batch_id):
            h.recalculate_batch(conn, int(old_batch_id))
        h.recalculate_batch(conn, int(batch_id))
        db.log_event(
            conn,
            entity_type="video",
            entity_id=int(deleted["id"]),
            action="deleted_resubmitted",
            actor_tg_id=actor.tg_id,
            actor_username=actor.username,
            before_data={"status": "deleted", "batch_id": old_batch_id},
            after_data={
                "status": "pending",
                "batch_id": batch_id,
                "video_type": h.normalize_video_type(data.get("video_type")),
            },
        )
        return h.get_video_by_id(conn, int(deleted["id"]))


def handle_message(message: dict[str, Any]) -> None:
    actor = h._actor_from_message(message)
    if not actor:
        return
    text = (message.get("text") or "").strip()
    tg = TelegramClient()

    if actor.tg_id == TARGET_USER_ID and text and _hearing_mode_enabled():
        _reply_like_bad_hearing(tg, message, actor)
        return

    if text.startswith("/"):
        command, rest = h._command_parts(text)
        if command == "/start" and rest.lower() in {"submit", "new_video", "new"}:
            h.db.clear_session(actor.tg_id)
            h.start_new_video(tg, actor)
            return
        if command == "/start" and rest.lower() in {"new_bigrecap", "bigrecap"}:
            h.db.clear_session(actor.tg_id)
            h.start_new_bigrecap(tg, actor)
            return
        if command == "/test_admin_chat":
            _test_admin_chat(tg, actor)
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
    if data == "cmd:test_admin_chat":
        tg = TelegramClient()
        try:
            tg.answer_callback_query(callback["id"])
        except Exception:
            pass
        _test_admin_chat(tg, actor)
        return
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
h.insert_pending_video = insert_pending_video
h.handle_message = handle_message
h.handle_callback = handle_callback

handle_update = h.handle_update
record_system_log = h.record_system_log
