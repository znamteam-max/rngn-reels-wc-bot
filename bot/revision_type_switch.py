from __future__ import annotations

from typing import Any

from bot import db
from bot import handlers as h
from bot import project_workflow_patch as workflow
from bot.telegram import TelegramClient, inline_keyboard


REVISION_KIND_FIELD = "revision_submission_kind"
REVISION_KIND_REGULAR = "regular"
REVISION_KIND_AIRCUT = "aircut"
VALID_REVISION_KINDS = {REVISION_KIND_REGULAR, REVISION_KIND_AIRCUT}
CALLBACK_PREFIX = "revkind:"
_INSTALLED = False


def revision_kind_for_video(video: dict[str, Any]) -> str:
    return REVISION_KIND_AIRCUT if workflow._is_aircut_video(video) else REVISION_KIND_REGULAR


def should_offer_type_switch(video: dict[str, Any] | None) -> bool:
    return bool(
        video
        and video.get("status") == "needs_revision"
        and str(video.get("project_code") or "") == workflow.VM_PROJECT_CODE
        and video.get("publish_date")
    )


def apply_revision_kind(data: dict[str, Any], kind: str) -> dict[str, Any]:
    if kind not in VALID_REVISION_KINDS:
        raise ValueError("unknown revision submission kind")
    updated = dict(data)
    updated[REVISION_KIND_FIELD] = kind
    if kind == REVISION_KIND_AIRCUT:
        updated["submission_kind"] = "aircut"
        updated["comment"] = workflow.AIR_CUT_MARKER
    else:
        updated.pop("submission_kind", None)
        updated.pop("comment", None)
    return updated


def _remove_aircut_marker(value: Any) -> str | None:
    text = str(value or "").strip()
    marker = workflow.AIR_CUT_MARKER
    if not text:
        return None
    if not text.startswith(marker):
        return text
    remainder = text[len(marker) :].strip()
    return remainder or None


def _revision_error(video: dict[str, Any] | None, actor: h.Actor) -> str | None:
    if not video:
        return "Заявка не найдена."
    if video.get("added_by_tg_id") != actor.tg_id and not h.is_admin(actor.tg_id):
        return "Можно исправлять только свои заявки."
    if video.get("status") != "needs_revision":
        return "Эта заявка сейчас не ожидает правку."
    if str(video.get("project_code") or "") != workflow.VM_PROJECT_CODE:
        return "Смена типа при правке доступна только для проекта «Взял Мяч»."
    if not video.get("publish_date"):
        return "Сначала укажите дату публикации."
    return None


def _type_keyboard(video_id: int, current_kind: str) -> dict[str, Any]:
    regular_label = "✅ Обычный ролик" if current_kind == REVISION_KIND_REGULAR else "Обычный ролик"
    aircut_label = "✅ Отрез из эфира" if current_kind == REVISION_KIND_AIRCUT else "Отрез из эфира"
    return inline_keyboard(
        [
            [(regular_label, f"{CALLBACK_PREFIX}{video_id}:{REVISION_KIND_REGULAR}")],
            [(aircut_label, f"{CALLBACK_PREFIX}{video_id}:{REVISION_KIND_AIRCUT}")],
        ]
    )


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    original_start_revision = h.start_revision
    original_handle_callback = h.handle_callback
    original_update_revision_video = h.update_revision_video

    def start_revision(tg: TelegramClient, actor: h.Actor, video_id: int) -> None:
        video = h.get_video_by_id_outside(video_id)
        if not should_offer_type_switch(video):
            original_start_revision(tg, actor, video_id)
            return
        error = _revision_error(video, actor)
        if error:
            tg.send_message(actor.chat_id, error)
            return
        current_kind = revision_kind_for_video(video)
        tg.send_message(
            actor.chat_id,
            f"Заявка #{video_id}: выберите тип после правки.\n\n"
            "Эта опция есть только при исправлении заявки «Взял Мяч».",
            _type_keyboard(video_id, current_kind),
        )

    def handle_callback(callback: dict[str, Any]) -> None:
        actor = h._actor_from_callback(callback)
        data = str(callback.get("data") or "")
        if not actor or not data.startswith(CALLBACK_PREFIX):
            original_handle_callback(callback)
            return

        parts = data.split(":")
        try:
            video_id = int(parts[1])
            kind = parts[2]
        except (IndexError, TypeError, ValueError):
            original_handle_callback(callback)
            return
        if kind not in VALID_REVISION_KINDS:
            original_handle_callback(callback)
            return

        tg = TelegramClient()
        video = h.get_video_by_id_outside(video_id)
        error = _revision_error(video, actor)
        if error:
            try:
                tg.answer_callback_query(str(callback.get("id") or ""), error, show_alert=True)
            except Exception:
                pass
            return

        try:
            tg.answer_callback_query(str(callback.get("id") or ""))
        except Exception:
            pass

        # Reuse the established revision flow, then add only the revision-only kind.
        original_start_revision(tg, actor, video_id)
        session = db.get_session(actor.tg_id)
        if not session:
            return
        session_data = dict(session.get("data") or {})
        if int(session_data.get("edit_video_id") or 0) != video_id:
            return
        session_data = apply_revision_kind(session_data, kind)
        db.set_session(
            tg_id=actor.tg_id,
            chat_id=actor.chat_id,
            username=actor.username,
            state=str(session.get("state") or "new:author"),
            data=session_data,
        )
        tg.send_message(
            actor.chat_id,
            "Тип после правки: "
            + ("Отрез из эфира." if kind == REVISION_KIND_AIRCUT else "Обычный ролик."),
        )

    def update_revision_video(
        actor: h.Actor,
        video_id: int,
        data: dict[str, Any],
    ) -> dict[str, Any]:
        kind = str(data.get(REVISION_KIND_FIELD) or "")
        before = h.get_video_by_id_outside(video_id)
        result = original_update_revision_video(actor, video_id, data)
        if kind not in VALID_REVISION_KINDS:
            return result

        desired_comment: str | None
        if kind == REVISION_KIND_AIRCUT:
            desired_comment = workflow.AIR_CUT_MARKER
        else:
            desired_comment = _remove_aircut_marker(result.get("comment"))

        current_comment = str(result.get("comment") or "").strip() or None
        if current_comment != desired_comment:
            db.execute(
                "UPDATE videos SET comment = %s, updated_at = now() WHERE id = %s",
                (desired_comment, int(video_id)),
            )
            refreshed = h.get_video_by_id_outside(video_id)
            if refreshed:
                result = refreshed

        before_kind = revision_kind_for_video(before) if before else None
        if before_kind != kind:
            try:
                with db.transaction() as conn:
                    db.log_event(
                        conn,
                        entity_type="video",
                        entity_id=video_id,
                        action="revision_type_changed",
                        actor_tg_id=actor.tg_id,
                        actor_username=actor.username,
                        before_data={"submission_kind": before_kind},
                        after_data={"submission_kind": kind},
                    )
            except Exception:
                pass
        return result

    h.start_revision = start_revision
    h.handle_callback = handle_callback
    h.update_revision_video = update_revision_video
    _INSTALLED = True
