from __future__ import annotations

from typing import Any

import psycopg
import requests

from bot import handlers as h
from bot.config import get_settings
from bot.telegram import TelegramAPIError, TelegramClient, inline_keyboard

_LAST_ADMIN_NOTIFY_ERROR: dict[str, Any] | None = None
_ORIGINAL_HANDLE_MESSAGE = h.handle_message
_ORIGINAL_HANDLE_CALLBACK = h.handle_callback


PUBLIC_START_TEXT = "Привет! Это бот для отчётов по рилзам ЧМ.\n\nВыбери действие:"
SOFT_ADMIN_FAIL_TEXT = (
    "Заявка сохранена, но сейчас не дошла до админского чата.\n"
    "Админ сможет переотправить её через /resend_pending."
)


def _request_with_parameters(self: TelegramClient, method: str, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        response = requests.post(
            f"{self.base_url}/{method}",
            json=payload,
            timeout=15,
        )
    except requests.RequestException as exc:
        raise RuntimeError("Telegram API request failed") from exc

    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError("Telegram API returned a non-JSON response") from exc

    if not data.get("ok"):
        description = data.get("description", "unknown Telegram API error")
        error = TelegramAPIError(description, response.status_code)
        setattr(error, "parameters", data.get("parameters") or {})
        raise error
    return data


TelegramClient._request = _request_with_parameters


def _migrate_to_chat_id(exc: Exception) -> int | None:
    params = getattr(exc, "parameters", None)
    if isinstance(params, dict) and params.get("migrate_to_chat_id") is not None:
        try:
            return int(params["migrate_to_chat_id"])
        except (TypeError, ValueError):
            return None
    return None


def telegram_failure_payload(exc: Exception, admin_chat_id: int, stage: str = "send") -> dict[str, Any]:
    payload = h.telegram_failure_payload(exc, admin_chat_id, stage)
    params = getattr(exc, "parameters", None)
    if isinstance(params, dict) and params:
        payload["telegram_parameters"] = params
        if params.get("migrate_to_chat_id") is not None:
            payload["migrate_to_chat_id"] = params.get("migrate_to_chat_id")
    return payload


h.telegram_failure_payload = telegram_failure_payload


def _send_admin_message(
    tg: TelegramClient,
    text: str,
    reply_markup: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], int, int | None]:
    settings = get_settings()
    try:
        response = tg.send_message(settings.admin_chat_id, text, reply_markup)
        return response, int(settings.admin_chat_id), None
    except Exception as exc:
        migrated_chat_id = _migrate_to_chat_id(exc)
        if not migrated_chat_id:
            raise
        response = tg.send_message(migrated_chat_id, text, reply_markup)
        return response, migrated_chat_id, migrated_chat_id


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
    text = f"Тест админского чата от @{actor.username}" if actor.username else f"Тест админского чата от {actor.tg_id}"
    try:
        response, used_chat_id, migrated_chat_id = _send_admin_message(tg, text)
        message_id = h._message_id(response)
        if migrated_chat_id:
            tg.send_message(
                actor.chat_id,
                "ADMIN_CHAT_ID устарел: группа была превращена в супергруппу.\n"
                f"Старый chat_id: {settings.admin_chat_id}\n"
                f"Новый chat_id: {migrated_chat_id}\n"
                f"Тестовое сообщение дошло. message_id: {message_id if message_id is not None else 'unknown'}\n\n"
                "Поставь новый chat_id в Vercel как ADMIN_CHAT_ID и сделай Redeploy.",
            )
        else:
            tg.send_message(
                actor.chat_id,
                "ADMIN_CHAT_ID работает.\n"
                f"chat_id: {used_chat_id}\n"
                f"message_id: {message_id if message_id is not None else 'unknown'}",
            )
    except Exception as exc:
        payload = telegram_failure_payload(exc, int(settings.admin_chat_id), "test_admin_chat")
        h.record_system_log("admin_chat_test_failed", "telegram_chat", None, payload, actor)
        migrated_chat_id = payload.get("migrate_to_chat_id")
        extra = f"\nНовый chat_id: {migrated_chat_id}" if migrated_chat_id else ""
        tg.send_message(
            actor.chat_id,
            "ADMIN_CHAT_ID не работает.\n"
            f"chat_id: {settings.admin_chat_id}\n"
            f"Telegram error: {payload.get('telegram_status_code', '?')} "
            f"{payload.get('telegram_description') or payload.get('error')}"
            f"{extra}",
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
        response, used_chat_id, migrated_chat_id = _send_admin_message(
            tg,
            h.format_video_card(video, title=title),
            h.admin_video_keyboard(int(video["id"]), int(video["batch_id"] or 0), 0),
        )
        h.store_admin_message(int(video["id"]), used_chat_id, response)
        if migrated_chat_id:
            h.record_system_log(
                "admin_chat_migrated",
                "video",
                int(video["id"]),
                {"old_admin_chat_id": settings.admin_chat_id, "migrate_to_chat_id": migrated_chat_id},
                actor,
            )
        _LAST_ADMIN_NOTIFY_ERROR = None
        return True
    except Exception as exc:
        payload = telegram_failure_payload(exc, int(settings.admin_chat_id), "send_review_card")
        if actor:
            payload["submitter_tg_id"] = actor.tg_id
            payload["submitter_username"] = actor.username
        _LAST_ADMIN_NOTIFY_ERROR = payload
        print(
            "admin_notify_failed "
            f"video_id={video.get('id')} chat_id={settings.admin_chat_id} "
            f"error={payload.get('telegram_status_code', '')} {payload.get('telegram_description') or payload.get('error')} "
            f"migrate_to_chat_id={payload.get('migrate_to_chat_id', '')}",
            flush=True,
        )
        h.record_system_log("admin_notify_failed", "video", int(video["id"]), payload, actor)
        return False


def send_admin_approved_card(
    tg: TelegramClient,
    video: dict[str, Any],
    actor: h.Actor,
    fallback_message_id: int | None = None,
) -> bool:
    settings = get_settings()
    text = h.format_final_card(video)
    stored_chat_id = video.get("admin_message_chat_id")
    stored_message_id = video.get("admin_message_id")
    edit_chat_id = int(stored_chat_id) if stored_chat_id else None
    edit_message_id = int(stored_message_id) if stored_message_id else None

    if not edit_chat_id and actor.chat_id == settings.admin_chat_id and fallback_message_id:
        edit_chat_id = actor.chat_id
        edit_message_id = fallback_message_id

    if edit_chat_id and edit_message_id:
        try:
            tg.edit_message_text(edit_chat_id, edit_message_id, text, {"inline_keyboard": []})
            h.store_admin_message(int(video["id"]), int(edit_chat_id), {"result": {"message_id": int(edit_message_id)}})
            return True
        except Exception as exc:
            h.record_system_log(
                "admin_notify_failed",
                "video",
                int(video["id"]),
                telegram_failure_payload(exc, int(settings.admin_chat_id), "edit_approved_card"),
                actor,
            )

    try:
        response, used_chat_id, _ = _send_admin_message(tg, text)
        h.store_admin_message(int(video["id"]), used_chat_id, response)
        return True
    except Exception as exc:
        h.record_system_log(
            "admin_notify_failed",
            "video",
            int(video["id"]),
            telegram_failure_payload(exc, int(settings.admin_chat_id), "send_approved_card"),
            actor,
        )
        return False


def send_admin_sync_warning(tg: TelegramClient, video: dict[str, Any], actor: h.Actor) -> None:
    settings = get_settings()
    try:
        _send_admin_message(
            tg,
            f"Заявка #{video['id']} одобрена, но Google Sheets не обновился. После исправления запустите /sync_sheets.",
        )
    except Exception as exc:
        h.record_system_log(
            "admin_notify_failed",
            "video",
            int(video["id"]),
            telegram_failure_payload(exc, int(settings.admin_chat_id), "send_sync_warning"),
            actor,
        )


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
        if last_error.get("migrate_to_chat_id"):
            text += f"\nНовый chat_id: {last_error['migrate_to_chat_id']}"
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
h.send_admin_approved_card = send_admin_approved_card
h.send_admin_sync_warning = send_admin_sync_warning
h.submit_video = submit_video
h.resend_pending_command = resend_pending_command
h.handle_message = handle_message
h.handle_callback = handle_callback

handle_update = h.handle_update
record_system_log = h.record_system_log
