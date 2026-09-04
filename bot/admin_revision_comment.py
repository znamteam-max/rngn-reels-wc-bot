from __future__ import annotations

from typing import Any

from bot import db, jobs
from bot import handlers as h
from bot import project_workflow_patch as workflow
from bot.telegram import TelegramClient


QUEUE_SESSION = "admin:revision_comment_queue"
LEGACY_SESSION = "admin:revision_comment_legacy"
REVISION_LABEL = "Комментарий администратора:"
REVISION_BLOCK = f"\n\n{REVISION_LABEL}\n"
MAX_COMMENT_LENGTH = 1500

_ACTIVE_REVISION_COMMENTS: dict[int, str] = {}
_INSTALLED = False


def normalize_comment(text: str) -> str:
    comment = str(text or "").strip()
    if not comment:
        raise ValueError("Комментарий не может быть пустым.")
    if len(comment) > MAX_COMMENT_LENGTH:
        raise ValueError(f"Комментарий слишком длинный. Максимум {MAX_COMMENT_LENGTH} символов.")
    return comment


def strip_revision_comment(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.startswith(f"{REVISION_LABEL}\n"):
        return None
    if REVISION_BLOCK in text:
        base = text.split(REVISION_BLOCK, 1)[0].rstrip()
        return base or None
    return text


def merge_revision_comment(value: Any, comment: str) -> str:
    base = strip_revision_comment(value)
    if base:
        return f"{base}{REVISION_BLOCK}{comment}"
    return f"{REVISION_LABEL}\n{comment}"


def author_revision_message(video_id: int, comment: str) -> str:
    return (
        f"🛠 Заявка #{video_id} возвращена на правку.\n\n"
        f"{REVISION_LABEL}\n{comment}\n\n"
        "Открой /my_requests и нажми «Исправить»."
    )


def _save_revision_comment(video_id: int, comment: str, actor: h.Actor) -> dict[str, Any] | None:
    with db.transaction() as conn:
        video = h.get_video_by_id(conn, video_id)
        if not video or video.get("status") != "needs_revision":
            return None
        stored_comment = merge_revision_comment(video.get("comment"), comment)
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE videos
                SET comment = %s,
                    updated_at = now()
                WHERE id = %s AND status = 'needs_revision'
                RETURNING id
                """,
                (stored_comment, video_id),
            )
            if not cur.fetchone():
                return None
        db.log_event(
            conn,
            entity_type="video",
            entity_id=video_id,
            action="revision_comment_added",
            actor_tg_id=actor.tg_id,
            actor_username=actor.username,
            before_data={"comment": video.get("comment")},
            after_data={"comment": comment},
        )
        return h.get_video_by_id(conn, video_id)


def _prompt_for_comment(
    tg: TelegramClient,
    actor: h.Actor,
    *,
    state: str,
    data: dict[str, Any],
) -> None:
    db.set_session(
        tg_id=actor.tg_id,
        chat_id=actor.chat_id,
        username=actor.username,
        state=state,
        data=data,
    )
    video_id = int(data.get("video_id") or 0)
    tg.send_message(
        actor.chat_id,
        f"Заявка #{video_id}: напишите одним сообщением, что нужно исправить.\n\n"
        "Только после комментария заявка будет возвращена автору.\n"
        "/cancel — отменить возврат.",
    )


def _start_queue_revision(
    tg: TelegramClient,
    actor: h.Actor,
    video_id: int,
    message_id: int | None,
    callback_id: str,
) -> None:
    if not h.is_admin(actor.tg_id):
        h._answer_queue_callback(
            tg,
            callback_id,
            "Это действие доступно только админам.",
            show_alert=True,
        )
        return
    with db.transaction() as conn:
        _, _, error = h._lock_current_queue_item(
            conn,
            video_id,
            actor.chat_id,
            message_id,
        )
    if error:
        h._answer_queue_callback(tg, callback_id, error, show_alert=True)
        return
    h._answer_queue_callback(tg, callback_id, "Введите комментарий к правке.")
    _prompt_for_comment(
        tg,
        actor,
        state=QUEUE_SESSION,
        data={
            "video_id": video_id,
            "message_id": int(message_id or 0),
            "callback_id": callback_id,
        },
    )


def _start_legacy_revision(
    tg: TelegramClient,
    actor: h.Actor,
    *,
    video_id: int,
    batch_id: int,
    index: int,
    message_id: int | None,
    callback_id: str,
) -> None:
    if not h.is_admin(actor.tg_id):
        h._answer_queue_callback(
            tg,
            callback_id,
            "Это действие доступно только админам.",
            show_alert=True,
        )
        return
    video = h.get_video_by_id_outside(video_id)
    if not video or video.get("status") != "pending":
        h._answer_queue_callback(tg, callback_id, "Заявка уже обработана.", show_alert=True)
        return
    h._answer_queue_callback(tg, callback_id, "Введите комментарий к правке.")
    _prompt_for_comment(
        tg,
        actor,
        state=LEGACY_SESSION,
        data={
            "video_id": video_id,
            "batch_id": batch_id,
            "index": index,
            "message_id": int(message_id or 0),
            "callback_id": callback_id,
        },
    )


def _finish_queue_revision(
    tg: TelegramClient,
    actor: h.Actor,
    data: dict[str, Any],
    comment: str,
) -> None:
    video_id = int(data.get("video_id") or 0)
    message_id = int(data.get("message_id") or 0)
    callback_id = str(data.get("callback_id") or "")
    _ACTIVE_REVISION_COMMENTS[video_id] = comment
    try:
        error, _ = h._process_admin_queue_action_v1018(
            tg,
            actor,
            video_id,
            message_id,
            "needs_revision",
            callback_id,
        )
    finally:
        _ACTIVE_REVISION_COMMENTS.pop(video_id, None)
    if error:
        db.clear_session(actor.tg_id)
        tg.send_message(actor.chat_id, error)
        return

    updated = _save_revision_comment(video_id, comment, actor)
    db.clear_session(actor.tg_id)
    if updated:
        h.sync_video_to_sheets(updated, actor, flow="revision_comment")
        try:
            tg.edit_message_text(
                actor.chat_id,
                message_id,
                h._format_processed_queue_card(updated, "needs_revision", actor)
                + f"\n{REVISION_LABEL} {comment}",
                {"inline_keyboard": []},
            )
        except Exception:
            pass
    tg.send_message(actor.chat_id, f"Комментарий сохранён. Заявка #{video_id} возвращена на правку.")


def _finish_legacy_revision(
    tg: TelegramClient,
    actor: h.Actor,
    data: dict[str, Any],
    comment: str,
) -> None:
    video_id = int(data.get("video_id") or 0)
    h.mark_video_status(
        tg,
        actor,
        video_id,
        int(data.get("batch_id") or 0),
        int(data.get("index") or 0),
        "needs_revision",
        author_revision_message(video_id, comment),
        int(data.get("message_id") or 0) or None,
    )
    updated = h.get_video_by_id_outside(video_id)
    if updated and updated.get("status") == "needs_revision":
        updated = _save_revision_comment(video_id, comment, actor)
        if updated:
            h.sync_video_to_sheets(updated, actor, flow="revision_comment_legacy")
    db.clear_session(actor.tg_id)


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    original_handle_admin_queue_callback = h.handle_admin_queue_callback
    original_handle_callback = h.handle_callback
    original_handle_message = h.handle_message
    original_update_revision_video = h.update_revision_video
    original_enqueue_notification = jobs.enqueue_telegram_notification

    def enqueue_telegram_notification(
        chat_id: int,
        text: str,
        *,
        event_key: str,
        reply_markup: dict[str, Any] | None = None,
        operation_id: int | None = None,
        available_at=None,
        priority: int = 80,
        conn=None,
    ) -> int | None:
        if event_key.startswith("queue-result:needs_revision:"):
            parts = event_key.split(":")
            try:
                video_id = int(parts[2])
            except (IndexError, TypeError, ValueError):
                video_id = 0
            comment = _ACTIVE_REVISION_COMMENTS.get(video_id)
            if comment:
                text = author_revision_message(video_id, comment)
        return original_enqueue_notification(
            chat_id,
            text,
            event_key=event_key,
            reply_markup=reply_markup,
            operation_id=operation_id,
            available_at=available_at,
            priority=priority,
            conn=conn,
        )

    def handle_admin_queue_callback(
        tg: TelegramClient,
        actor: h.Actor,
        data: str,
        message_id: int | None,
        callback_id: str,
    ) -> None:
        parts = str(data or "").split(":")
        if len(parts) >= 3 and parts[0] == "admq" and parts[1] == "revision":
            try:
                video_id = int(parts[2])
            except (TypeError, ValueError):
                h._answer_queue_callback(tg, callback_id, h.ADMIN_QUEUE_STALE_MESSAGE, show_alert=True)
                return
            _start_queue_revision(tg, actor, video_id, message_id, callback_id)
            return
        original_handle_admin_queue_callback(tg, actor, data, message_id, callback_id)

    def handle_callback(callback: dict[str, Any]) -> None:
        actor = h._actor_from_callback(callback)
        data = str(callback.get("data") or "")
        if actor and data.startswith("adm:r:"):
            parts = data.split(":")
            try:
                video_id = int(parts[2])
                batch_id = int(parts[3])
                index = int(parts[4])
            except (IndexError, TypeError, ValueError):
                original_handle_callback(callback)
                return
            message = callback.get("message") or {}
            _start_legacy_revision(
                TelegramClient(),
                actor,
                video_id=video_id,
                batch_id=batch_id,
                index=index,
                message_id=message.get("message_id"),
                callback_id=str(callback.get("id") or ""),
            )
            return
        original_handle_callback(callback)

    def handle_message(message: dict[str, Any]) -> None:
        actor = h._actor_from_message(message)
        text = str(message.get("text") or "").strip()
        if not actor:
            original_handle_message(message)
            return
        session = db.get_session(actor.tg_id)
        state = str(session.get("state") or "") if session else ""
        if state not in {QUEUE_SESSION, LEGACY_SESSION}:
            original_handle_message(message)
            return
        if text.startswith("/"):
            original_handle_message(message)
            return
        if not h.is_admin(actor.tg_id):
            db.clear_session(actor.tg_id)
            TelegramClient().send_message(actor.chat_id, "Комментарий к правке может отправить только админ.")
            return
        try:
            comment = normalize_comment(text)
        except ValueError as exc:
            TelegramClient().send_message(actor.chat_id, str(exc))
            return
        tg = TelegramClient()
        if state == QUEUE_SESSION:
            _finish_queue_revision(tg, actor, dict(session.get("data") or {}), comment)
        else:
            _finish_legacy_revision(tg, actor, dict(session.get("data") or {}), comment)

    def update_revision_video(actor: h.Actor, video_id: int, data: dict[str, Any]) -> dict[str, Any]:
        before = h.get_video_by_id_outside(video_id)
        result = original_update_revision_video(actor, video_id, data)
        old_comment = before.get("comment") if before else None
        cleaned = strip_revision_comment(old_comment)
        if cleaned != (str(old_comment).strip() if old_comment else None):
            db.execute(
                "UPDATE videos SET comment = %s, updated_at = now() WHERE id = %s",
                (cleaned, video_id),
            )
            refreshed = h.get_video_by_id_outside(video_id)
            if refreshed:
                return refreshed
        return result

    def is_aircut_video(video: dict[str, Any]) -> bool:
        return str(video.get("comment") or "").strip().startswith(workflow.AIR_CUT_MARKER)

    jobs.enqueue_telegram_notification = enqueue_telegram_notification
    h.handle_admin_queue_callback = handle_admin_queue_callback
    h.handle_callback = handle_callback
    h.handle_message = handle_message
    h.update_revision_video = update_revision_video
    workflow._is_aircut_video = is_aircut_video

    _INSTALLED = True
