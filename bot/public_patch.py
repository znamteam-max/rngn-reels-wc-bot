from __future__ import annotations

from typing import Any

import psycopg

from bot import handlers as h
from bot.config import get_settings
from bot.telegram import TelegramClient, inline_keyboard

_LAST_ADMIN_NOTIFY_ERROR: dict[str, Any] | None = None
_ORIGINAL_HANDLE_MESSAGE = h.handle_message
_ORIGINAL_HANDLE_CALLBACK = h.handle_callback


PUBLIC_START_TEXT = "Привет! Это бот для отчётов по рилзам ЧМ.\n\nВыбери действие:"
SOFT_ADMIN_FAIL_TEXT = (
    "Заявка сохранена, но сейчас не дошла до админского чата.\n"
    "Админ сможет переотправить её через /resend_pending."
)


def _send_main_menu(tg: TelegramClient, actor: h.Actor, text: str = PUBLIC_START_TEXT) -> None:
    rows = [
        [("Новое видео", "cmd:new"), ("Мои заявки", "cmd:my")],
        [("Помощь", "cmd:help")],
    ]
    if h.is_admin(actor.tg_id):
        rows.insert(1, [("Админка", "cmd:admin"), ("Сводка", "cmd:summary")])
        rows.insert(2, [("Переотправить pending", "cmd:resend_pending"), ("Тест админ-чата", "cmd:test_admin_chat")])
    tg.send_message(actor.chat_id, text, inline_keyboard(rows))


def require_admin(tg: TelegramClient, actor: h.Actor) -> bool:
    if not h.is_admin(actor.tg_id):
        tg.send_message(actor.chat_id, "Нет доступа к админским действиям.")
        return False
    return True


def test_admin_chat_command(tg: TelegramClient, actor: h.Actor) -> None:
    if not require_admin(tg, actor):
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


def send_admin_review_card(
    tg: TelegramClient,
    video: dict[str, Any],
    title: str = "Заявка",
    actor: h.Actor | None = None,
) -> bool:
    global _LAST_ADMIN_NOTIFY_ERROR
    settings = get_settings()
    try:
        response = tg.send_message(
            settings.admin_chat_id,
            h.format_video_card(video, title=title),
            h.admin_video_keyboard(int(video["id"]), int(video["batch_id"] or 0), 0),
        )
        h.store_admin_message(int(video["id"]), int(settings.admin_chat_id), response)
        _LAST_ADMIN_NOTIFY_ERROR = None
        return True
    except Exception as exc:
        payload = h.telegram_failure_payload(exc, int(settings.admin_chat_id), "send_review_card")
        if actor:
            payload["submitter_tg_id"] = actor.tg_id
            payload["submitter_username"] = actor.username
        _LAST_ADMIN_NOTIFY_ERROR = payload
        print(
            "admin_notify_failed "
            f"video_id={video.get('id')} chat_id={settings.admin_chat_id} "
            f"error={payload.get('telegram_status_code', '')} {payload.get('telegram_description') or payload.get('error')}",
            flush=True,
        )
        h.record_system_log("admin_notify_failed", "video", int(video["id"]), payload, actor)
        return False


def submit_video(tg: TelegramClient, actor: h.Actor) -> None:
    session = h.db.get_session(actor.tg_id)
    if not session:
        tg.send_message(actor.chat_id, "Начните заявку заново: /new_video.")
        return
    data = session.get("data") or {}
    if not data.get("instagram_id"):
        tg.send_message(actor.chat_id, "В заявке нет Instagram ID. Начните заново: /new_video.")
        return

    edit_video_id = int(data["edit_video_id"]) if data.get("edit_video_id") else None
    duplicate = h.find_video_by_instagram_id(data["instagram_id"])
    if duplicate and duplicate.get("id") != edit_video_id:
        h.db.clear_session(actor.tg_id)
        tg.send_message(actor.chat_id, h.format_video_card(duplicate, title="Такое видео уже есть"))
        return

    try:
        if edit_video_id:
            video = h.update_revision_video(actor, edit_video_id, data)
        else:
            video = h.insert_pending_video(actor, data)
    except psycopg.errors.UniqueViolation:
        duplicate = h.find_video_by_instagram_id(data["instagram_id"])
        h.db.clear_session(actor.tg_id)
        if duplicate:
            tg.send_message(actor.chat_id, h.format_video_card(duplicate, title="Такое видео уже есть"))
        else:
            tg.send_message(actor.chat_id, "Похоже, заявка уже была добавлена. Проверьте /my_requests.")
        return
    except RuntimeError as exc:
        h.db.clear_session(actor.tg_id)
        tg.send_message(actor.chat_id, h._safe_error(exc))
        return

    h.db.clear_session(actor.tg_id)
    if h.notify_admin_queue(tg, video, actor):
        tg.send_message(actor.chat_id, "Заявка отправлена на проверку.")
    else:
        tg.send_message(actor.chat_id, SOFT_ADMIN_FAIL_TEXT)


def resend_pending_command(tg: TelegramClient, actor: h.Actor) -> None:
    if not require_admin(tg, actor):
        return
    with h.db.transaction() as conn:
        h.assign_orphan_pending(conn, actor)
    rows = h.db.fetch_all(h.PENDING_VIDEOS_SQL)
    if not rows:
        tg.send_message(actor.chat_id, "Pending-заявок нет.")
        return

    tg.send_message(actor.chat_id, f"Найдено pending-заявок: {len(rows)}\nПереотправляю в админский чат...")
    sent = 0
    failed = 0
    last_error: dict[str, Any] | None = None
    for row in rows:
        if send_admin_review_card(tg, row, "Pending-заявка", actor):
            sent += 1
        else:
            failed += 1
            last_error = _LAST_ADMIN_NOTIFY_ERROR

    text = f"Переотправлено: {sent}\nОшибок: {failed}"
    if last_error:
        text += "\n\nПоследняя ошибка:\n"
        if last_error.get("telegram_status_code") or last_error.get("telegram_description"):
            text += f"{last_error.get('telegram_status_code', '?')} {last_error.get('telegram_description', '')}".strip()
        else:
            text += str(last_error.get("error", "unknown"))
    tg.send_message(actor.chat_id, text)


def handle_message(message: dict[str, Any]) -> None:
    actor = h._actor_from_message(message)
    if not actor:
        return
    text = (message.get("text") or "").strip()
    if not text:
        return

    if text.startswith("/"):
        command, rest = h._command_parts(text)
        tg = TelegramClient()
        if command == "/start":
            h.db.clear_session(actor.tg_id)
            if rest.lower() in {"submit", "new_video", "new"}:
                h.start_new_video(tg, actor)
            else:
                _send_main_menu(tg, actor, PUBLIC_START_TEXT)
            return
        if command == "/test_admin_chat":
            test_admin_chat_command(tg, actor)
            return

    _ORIGINAL_HANDLE_MESSAGE(message)


def handle_callback(callback: dict[str, Any]) -> None:
    actor = h._actor_from_callback(callback)
    data = callback.get("data") or ""
    if actor and data == "cmd:test_admin_chat":
        tg = TelegramClient()
        try:
            tg.answer_callback_query(callback["id"])
        except Exception:
            pass
        test_admin_chat_command(tg, actor)
        return
    _ORIGINAL_HANDLE_CALLBACK(callback)


# Monkey-patch the original module globals used by existing handlers.
h._send_main_menu = _send_main_menu
h.require_admin = require_admin
h.test_admin_chat_command = test_admin_chat_command
h.send_admin_review_card = send_admin_review_card
h.submit_video = submit_video
h.resend_pending_command = resend_pending_command
h.handle_message = handle_message
h.handle_callback = handle_callback

handle_update = h.handle_update
record_system_log = h.record_system_log
