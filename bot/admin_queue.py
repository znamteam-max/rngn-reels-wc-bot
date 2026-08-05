from __future__ import annotations

import hashlib
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Callable, Iterator

from bot import db, jobs
from bot.config import get_settings


ADMIN_QUEUE_NAME = "main"
RESERVATION_STALE_SECONDS = 5
QUEUE_FILTER_TYPES = {"global", "project", "other", "unassigned"}

_TRACE_ID: ContextVar[str | None] = ContextVar("admin_queue_trace_id", default=None)


@dataclass(frozen=True)
class QueueReservation:
    queue_name: str
    video_id: int
    chat_id: int
    token: str
    generation: int
    reserved_at: datetime
    delivery_attempts: int
    reason: str
    should_deliver: bool
    active_message_id: int | None = None
    pending_count: int = 0
    global_pending_count: int = 0
    filter_type: str = "global"
    filter_value: str | None = None
    reserve_duration_ms: int = 0


@dataclass(frozen=True)
class QueueDeliveryResult:
    video_id: int
    sent: bool
    pointer_saved: bool
    message_id: int | None
    error: str | None = None
    repair_job_id: int | None = None
    send_duration_ms: int = 0
    save_duration_ms: int = 0


@dataclass(frozen=True)
class QueueRepairResult:
    repaired: bool
    reason: str
    active_video_id: int | None
    active_message_id: int | None
    stale_metadata_cleared: int = 0
    pump_needed: bool = False
    pump_result: dict[str, Any] | None = None
    adopted_message: bool = False
    repair_job_id: int | None = None


@dataclass
class CompletionResult:
    accepted: bool
    video_id: int
    action: str
    video: dict[str, Any] | None = None
    error: str | None = None
    old_active_video_id: int | None = None
    generation: int = 0
    trace_id: str | None = None
    timings_ms: dict[str, int] = field(default_factory=dict)


def derive_trace_id(update_id: Any, callback_query_id: Any) -> str:
    raw = f"{update_id or ''}:{callback_query_id or ''}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


@contextmanager
def trace_scope(update_id: Any, callback_query_id: Any) -> Iterator[str]:
    trace_id = derive_trace_id(update_id, callback_query_id)
    token: Token[str | None] = _TRACE_ID.set(trace_id)
    try:
        yield trace_id
    finally:
        _TRACE_ID.reset(token)


def current_trace_id() -> str | None:
    return _TRACE_ID.get()


def _safe_error(exc: Exception | str) -> str:
    text = str(exc)
    settings = get_settings()
    for secret in (settings.bot_token, settings.database_url, settings.cron_secret):
        if secret:
            text = text.replace(secret, "[secret]")
    return text[:500]


def _actor_value(actor: Any, name: str) -> Any:
    return getattr(actor, name, None) if actor is not None else None


def _log_event(
    conn,
    action: str,
    *,
    video_id: int | None = None,
    actor: Any = None,
    trace_id: str | None = None,
    data: dict[str, Any] | None = None,
) -> None:
    payload = dict(data or {})
    effective_trace = trace_id or current_trace_id()
    if effective_trace:
        payload["trace_id"] = effective_trace
    db.log_event(
        conn,
        entity_type="admin_queue",
        entity_id=video_id,
        action=action,
        actor_tg_id=_actor_value(actor, "tg_id"),
        actor_username=_actor_value(actor, "username"),
        after_data=payload,
    )


def _log_standalone(
    action: str,
    *,
    video_id: int | None = None,
    actor: Any = None,
    trace_id: str | None = None,
    data: dict[str, Any] | None = None,
) -> None:
    try:
        with db.transaction() as conn:
            _log_event(
                conn,
                action,
                video_id=video_id,
                actor=actor,
                trace_id=trace_id,
                data=data,
            )
    except Exception:
        pass


def queue_state_for_update(conn, queue_name: str = ADMIN_QUEUE_NAME) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO admin_queue_state (queue_name) VALUES (%s) "
            "ON CONFLICT (queue_name) DO NOTHING",
            (queue_name,),
        )
        cur.execute(
            "SELECT * FROM admin_queue_state WHERE queue_name = %s FOR UPDATE",
            (queue_name,),
        )
        state = cur.fetchone()
    if not state:
        raise RuntimeError("Admin queue state is unavailable")
    return state


def read_queue_state(conn, queue_name: str = ADMIN_QUEUE_NAME) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM admin_queue_state WHERE queue_name = %s", (queue_name,))
        state = cur.fetchone()
    if state:
        return state
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO admin_queue_state (queue_name) VALUES (%s) "
            "ON CONFLICT (queue_name) DO NOTHING",
            (queue_name,),
        )
        cur.execute("SELECT * FROM admin_queue_state WHERE queue_name = %s", (queue_name,))
        state = cur.fetchone()
    if not state:
        raise RuntimeError("Admin queue state is unavailable")
    return state


def queue_filter(state: dict[str, Any] | None) -> tuple[str, str | None]:
    filter_type = str((state or {}).get("queue_filter_type") or "global")
    filter_value = (state or {}).get("queue_filter_value")
    if filter_type not in QUEUE_FILTER_TYPES:
        return "global", None
    if filter_type == "project" and not filter_value:
        return "global", None
    return filter_type, str(filter_value) if filter_value else None


def queue_filter_sql(
    state: dict[str, Any] | None,
    *,
    alias: str = "v",
) -> tuple[str, tuple[Any, ...]]:
    filter_type, filter_value = queue_filter(state)
    if filter_type == "project":
        return f"{alias}.project_code = %s", (filter_value,)
    if filter_type == "other":
        return f"{alias}.project_code = 'other'", ()
    if filter_type == "unassigned":
        return (
            f"(COALESCE({alias}.project_code, '') = '' "
            f"OR COALESCE({alias}.project_name, '') = '')",
            (),
        )
    return "TRUE", ()


def video_matches_filter(video: dict[str, Any], state: dict[str, Any] | None) -> bool:
    filter_type, filter_value = queue_filter(state)
    project_code = str(video.get("project_code") or "")
    project_name = str(video.get("project_name") or "")
    if filter_type == "project":
        return project_code == filter_value
    if filter_type == "other":
        return project_code == "other"
    if filter_type == "unassigned":
        return not project_code or not project_name
    return True


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _reservation_is_recent(state: dict[str, Any]) -> bool:
    reserved_at = _aware(state.get("active_reserved_at"))
    return bool(
        state.get("active_reservation_token")
        and reserved_at
        and reserved_at > datetime.now(timezone.utc) - timedelta(seconds=RESERVATION_STALE_SECONDS)
    )


def _count_pending(conn, state: dict[str, Any] | None = None) -> tuple[int, int]:
    condition, params = queue_filter_sql(state)
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT count(*) AS count FROM videos v "
            f"WHERE v.status = 'pending' AND {condition}",
            params,
        )
        filtered = int(cur.fetchone()["count"])
        cur.execute("SELECT count(*) AS count FROM videos WHERE status = 'pending'")
        global_count = int(cur.fetchone()["count"])
    return filtered, global_count


def _clear_active_fields(
    conn,
    *,
    queue_name: str,
    repair_reason: str | None = None,
    clear_error: bool = False,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE admin_queue_state
            SET active_video_id = NULL,
                active_chat_id = NULL,
                active_message_id = NULL,
                active_reservation_token = NULL,
                active_reserved_at = NULL,
                claimed_by_tg_id = NULL,
                claimed_by_username = NULL,
                claimed_at = NULL,
                active_last_error = CASE WHEN %s THEN NULL ELSE active_last_error END,
                active_last_error_at = CASE WHEN %s THEN NULL ELSE active_last_error_at END,
                last_repaired_at = CASE WHEN %s::text IS NOT NULL THEN now() ELSE last_repaired_at END,
                last_repair_reason = COALESCE(%s, last_repair_reason),
                updated_at = now()
            WHERE queue_name = %s
            """,
            (clear_error, clear_error, repair_reason, repair_reason, queue_name),
        )


def clear_active_pointer(
    conn,
    *,
    queue_name: str = ADMIN_QUEUE_NAME,
    reason: str | None = None,
) -> None:
    _clear_active_fields(conn, queue_name=queue_name, repair_reason=reason)


def claim_active_for_date(
    conn,
    *,
    actor: Any,
    queue_name: str = ADMIN_QUEUE_NAME,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE admin_queue_state
            SET claimed_by_tg_id = %s,
                claimed_by_username = %s,
                claimed_at = now(),
                updated_at = now()
            WHERE queue_name = %s
            """,
            (_actor_value(actor, "tg_id"), _actor_value(actor, "username"), queue_name),
        )


def release_active_claim(conn, *, queue_name: str = ADMIN_QUEUE_NAME) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE admin_queue_state
            SET claimed_by_tg_id = NULL,
                claimed_by_username = NULL,
                claimed_at = NULL,
                updated_at = now()
            WHERE queue_name = %s
            """,
            (queue_name,),
        )


def _reservation_from_state(
    state: dict[str, Any],
    *,
    reason: str,
    pending_count: int,
    global_pending_count: int,
    should_deliver: bool,
    reserve_duration_ms: int,
) -> QueueReservation:
    filter_type, filter_value = queue_filter(state)
    return QueueReservation(
        queue_name=str(state.get("queue_name") or ADMIN_QUEUE_NAME),
        video_id=int(state["active_video_id"]),
        chat_id=int(state.get("active_chat_id") or get_settings().admin_chat_id),
        token=str(state["active_reservation_token"]),
        generation=int(state.get("active_generation") or 0),
        reserved_at=_aware(state.get("active_reserved_at")) or datetime.now(timezone.utc),
        delivery_attempts=int(state.get("active_delivery_attempts") or 0),
        reason=reason,
        should_deliver=should_deliver,
        active_message_id=int(state["active_message_id"])
        if state.get("active_message_id")
        else None,
        pending_count=pending_count,
        global_pending_count=global_pending_count,
        filter_type=filter_type,
        filter_value=filter_value,
        reserve_duration_ms=reserve_duration_ms,
    )


def reserve_next_pending_card(
    conn,
    *,
    queue_name: str = ADMIN_QUEUE_NAME,
    reason: str,
) -> QueueReservation | None:
    started = time.monotonic()
    state = queue_state_for_update(conn, queue_name)
    previous_active_video_id = (
        int(state["active_video_id"]) if state.get("active_video_id") else None
    )
    pending_count, global_pending_count = _count_pending(conn, state)
    active_video: dict[str, Any] | None = None
    if previous_active_video_id:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, status, project_code, project_name,
                       admin_message_chat_id, admin_message_id
                FROM videos
                WHERE id = %s
                FOR UPDATE
                """,
                (previous_active_video_id,),
            )
            active_video = cur.fetchone()

    active_is_pending = bool(
        active_video
        and active_video.get("status") == "pending"
        and video_matches_filter(active_video, state)
    )
    active_message_is_valid = bool(
        active_is_pending
        and state.get("active_message_id")
        and int(state.get("active_chat_id") or 0) == int(get_settings().admin_chat_id)
    )

    if active_message_is_valid:
        token = state.get("active_reservation_token") or uuid.uuid4()
        generation = max(1, int(state.get("active_generation") or 0))
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE admin_queue_state
                SET active_reservation_token = %s,
                    active_reserved_at = COALESCE(active_reserved_at, now()),
                    active_generation = %s,
                    active_delivery_attempts = GREATEST(active_delivery_attempts, 1),
                    updated_at = now()
                WHERE queue_name = %s
                RETURNING *
                """,
                (token, generation, queue_name),
            )
            state = cur.fetchone()
            cur.execute(
                """
                UPDATE videos
                SET admin_message_chat_id = %s,
                    admin_message_id = %s,
                    admin_notified_at = COALESCE(admin_notified_at, now()),
                    updated_at = now()
                WHERE id = %s AND status = 'pending'
                """,
                (state["active_chat_id"], state["active_message_id"], previous_active_video_id),
            )
        return _reservation_from_state(
            state,
            reason=reason,
            pending_count=pending_count,
            global_pending_count=global_pending_count,
            should_deliver=False,
            reserve_duration_ms=int((time.monotonic() - started) * 1000),
        )

    if active_is_pending and not state.get("active_message_id") and _reservation_is_recent(state):
        return _reservation_from_state(
            state,
            reason=reason,
            pending_count=pending_count,
            global_pending_count=global_pending_count,
            should_deliver=False,
            reserve_duration_ms=int((time.monotonic() - started) * 1000),
        )

    selected_id: int | None = None
    if active_is_pending and not state.get("active_message_id"):
        selected_id = previous_active_video_id
    else:
        if previous_active_video_id or state.get("active_message_id"):
            _clear_active_fields(
                conn,
                queue_name=queue_name,
                repair_reason="invalid active pointer cleared before reserve",
            )
        condition, params = queue_filter_sql(state)
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT v.id
                FROM videos v
                WHERE v.status = 'pending'
                  AND {condition}
                ORDER BY v.created_at ASC, v.id ASC
                FOR UPDATE SKIP LOCKED
                LIMIT 1
                """,
                params,
            )
            selected = cur.fetchone()
        selected_id = int(selected["id"]) if selected else None

    if selected_id is None:
        _clear_active_fields(conn, queue_name=queue_name)
        return None

    reservation_token = uuid.uuid4()
    chat_id = int(get_settings().admin_chat_id)
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE admin_queue_state
            SET active_video_id = %s,
                active_chat_id = %s,
                active_message_id = NULL,
                active_reservation_token = %s,
                active_reserved_at = now(),
                active_generation = active_generation + 1,
                active_delivery_attempts = active_delivery_attempts + 1,
                claimed_by_tg_id = NULL,
                claimed_by_username = NULL,
                claimed_at = NULL,
                active_last_error = NULL,
                active_last_error_at = NULL,
                updated_at = now()
            WHERE queue_name = %s
            RETURNING *
            """,
            (selected_id, chat_id, reservation_token, queue_name),
        )
        state = cur.fetchone()
    duration_ms = int((time.monotonic() - started) * 1000)
    _log_event(
        conn,
        "queue_next_reserved",
        video_id=selected_id,
        data={
            "video_id": selected_id,
            "generation": int(state.get("active_generation") or 0),
            "reason": reason,
            "previous_active_video_id": previous_active_video_id,
            "duration_ms": duration_ms,
        },
    )
    return _reservation_from_state(
        state,
        reason=reason,
        pending_count=pending_count,
        global_pending_count=global_pending_count,
        should_deliver=True,
        reserve_duration_ms=duration_ms,
    )


def _message_id(response: dict[str, Any]) -> int | None:
    result = response.get("result") if isinstance(response, dict) else None
    if not isinstance(result, dict) or result.get("message_id") is None:
        return None
    return int(result["message_id"])


def _load_delivery_payload(conn, reservation: QueueReservation) -> tuple[dict[str, Any], str, Any]:
    from bot import handlers as h

    state = read_queue_state(conn, reservation.queue_name)
    video = h.get_video_by_id(conn, reservation.video_id)
    total = h._pending_video_count(conn, state)
    position = h._queue_position(conn, video, state)
    text = h.format_admin_queue_card(
        video,
        total,
        position,
        h._queue_filter_label(state),
    )
    return video, text, h.admin_queue_keyboard(reservation.video_id)


def _save_pointer_message(
    conn,
    reservation: QueueReservation,
    *,
    chat_id: int,
    message_id: int,
    actor: Any = None,
) -> bool:
    started = time.monotonic()
    state = queue_state_for_update(conn, reservation.queue_name)
    token_matches = str(state.get("active_reservation_token") or "") == reservation.token
    pointer_matches = bool(
        int(state.get("active_video_id") or 0) == reservation.video_id
        and int(state.get("active_generation") or 0) == reservation.generation
        and token_matches
    )
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM videos WHERE id = %s FOR UPDATE", (reservation.video_id,))
        video = cur.fetchone()
    if not pointer_matches or not video or video.get("status") != "pending":
        return False
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE admin_queue_state
            SET active_chat_id = %s,
                active_message_id = %s,
                active_reserved_at = now(),
                active_last_error = NULL,
                active_last_error_at = NULL,
                updated_at = now()
            WHERE queue_name = %s
            """,
            (chat_id, message_id, reservation.queue_name),
        )
        cur.execute(
            """
            UPDATE videos
            SET admin_message_chat_id = %s,
                admin_message_id = %s,
                admin_notified_at = now(),
                updated_at = now()
            WHERE id = %s AND status = 'pending'
            """,
            (chat_id, message_id, reservation.video_id),
        )
    _log_event(
        conn,
        "queue_pointer_message_saved",
        video_id=reservation.video_id,
        actor=actor,
        data={
            "video_id": reservation.video_id,
            "generation": reservation.generation,
            "old_active_video_id": reservation.video_id,
            "new_active_video_id": reservation.video_id,
            "duration_ms": int((time.monotonic() - started) * 1000),
        },
    )
    return True


def _release_failed_reservation(
    reservation: QueueReservation,
    error: str,
    *,
    actor: Any = None,
) -> int | None:
    job_id: int | None = None
    with db.transaction() as conn:
        state = queue_state_for_update(conn, reservation.queue_name)
        matches = bool(
            int(state.get("active_video_id") or 0) == reservation.video_id
            and int(state.get("active_generation") or 0) == reservation.generation
            and str(state.get("active_reservation_token") or "") == reservation.token
        )
        if matches:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE admin_queue_state
                    SET active_video_id = NULL,
                        active_chat_id = NULL,
                        active_message_id = NULL,
                        active_reservation_token = NULL,
                        active_reserved_at = NULL,
                        claimed_by_tg_id = NULL,
                        claimed_by_username = NULL,
                        claimed_at = NULL,
                        active_last_error = %s,
                        active_last_error_at = now(),
                        updated_at = now()
                    WHERE queue_name = %s
                    """,
                    (error, reservation.queue_name),
                )
            job_id = jobs.enqueue_admin_queue_pump(conn=conn)
        _log_event(
            conn,
            "queue_card_send_failed",
            video_id=reservation.video_id,
            actor=actor,
            data={
                "video_id": reservation.video_id,
                "generation": reservation.generation,
                "error": error,
            },
        )
    if job_id is not None:
        try:
            from bot.worker_kick import kick_worker_if_ready

            kick_worker_if_ready(reason="queue_card_send_failed")
        except Exception:
            pass
    return job_id


def _adoption_payload(
    reservation: QueueReservation,
    *,
    chat_id: int,
    message_id: int,
) -> dict[str, Any]:
    return {
        "video_id": reservation.video_id,
        "chat_id": chat_id,
        "message_id": message_id,
        "reservation_token": reservation.token,
        "generation": reservation.generation,
        "queue_name": reservation.queue_name,
    }


def deliver_reserved_card(
    tg,
    reservation: QueueReservation,
    *,
    actor: Any = None,
    conn=None,
) -> QueueDeliveryResult:
    if not reservation.should_deliver:
        return QueueDeliveryResult(
            video_id=reservation.video_id,
            sent=False,
            pointer_saved=bool(reservation.active_message_id),
            message_id=reservation.active_message_id,
        )

    if conn is None:
        with db.connect() as read_conn:
            video, text, keyboard = _load_delivery_payload(read_conn, reservation)
    else:
        video, text, keyboard = _load_delivery_payload(conn, reservation)
    if video.get("status") != "pending":
        error = "reserved video is no longer pending"
        job_id = None
        if conn is None:
            try:
                job_id = _release_failed_reservation(reservation, error, actor=actor)
            except Exception as release_exc:
                _log_standalone(
                    "queue_card_send_failed",
                    video_id=reservation.video_id,
                    actor=actor,
                    data={
                        "video_id": reservation.video_id,
                        "generation": reservation.generation,
                        "error": error,
                        "release_error": _safe_error(release_exc),
                    },
                )
        return QueueDeliveryResult(
            video_id=reservation.video_id,
            sent=False,
            pointer_saved=False,
            message_id=None,
            error=error,
            repair_job_id=job_id,
        )

    send_started = time.monotonic()
    if conn is None:
        _log_standalone(
            "queue_card_send_started",
            video_id=reservation.video_id,
            actor=actor,
            data={"video_id": reservation.video_id, "generation": reservation.generation},
        )
    else:
        _log_event(
            conn,
            "queue_card_send_started",
            video_id=reservation.video_id,
            actor=actor,
            data={"video_id": reservation.video_id, "generation": reservation.generation},
        )
    try:
        response = tg.send_message(reservation.chat_id, text, keyboard)
        message_id = _message_id(response)
        if not message_id:
            raise RuntimeError("Telegram did not return a message_id for the admin queue card")
    except Exception as exc:
        error = _safe_error(exc)
        job_id = None
        if conn is None:
            try:
                job_id = _release_failed_reservation(reservation, error, actor=actor)
            except Exception as release_exc:
                _log_standalone(
                    "queue_card_send_failed",
                    video_id=reservation.video_id,
                    actor=actor,
                    data={
                        "video_id": reservation.video_id,
                        "generation": reservation.generation,
                        "error": error,
                        "release_error": _safe_error(release_exc),
                    },
                )
        return QueueDeliveryResult(
            video_id=reservation.video_id,
            sent=False,
            pointer_saved=False,
            message_id=None,
            error=error,
            repair_job_id=job_id,
            send_duration_ms=int((time.monotonic() - send_started) * 1000),
        )

    send_duration_ms = int((time.monotonic() - send_started) * 1000)
    event_data = {
        "video_id": reservation.video_id,
        "generation": reservation.generation,
        "duration_ms": send_duration_ms,
    }
    if conn is None:
        _log_standalone(
            "queue_card_sent",
            video_id=reservation.video_id,
            actor=actor,
            data=event_data,
        )
    else:
        _log_event(
            conn,
            "queue_card_sent",
            video_id=reservation.video_id,
            actor=actor,
            data=event_data,
        )

    save_started = time.monotonic()
    try:
        if conn is None:
            with db.transaction() as save_conn:
                pointer_saved = _save_pointer_message(
                    save_conn,
                    reservation,
                    chat_id=reservation.chat_id,
                    message_id=message_id,
                    actor=actor,
                )
        else:
            pointer_saved = _save_pointer_message(
                conn,
                reservation,
                chat_id=reservation.chat_id,
                message_id=message_id,
                actor=actor,
            )
        if not pointer_saved:
            raise RuntimeError("active reservation changed before pointer save")
    except Exception as exc:
        error = _safe_error(exc)
        repair_job_id = None
        if conn is None:
            enqueue_error = None
            try:
                repair_job_id = jobs.enqueue_admin_queue_pump(
                    adopt_message=_adoption_payload(
                        reservation,
                        chat_id=reservation.chat_id,
                        message_id=message_id,
                    )
                )
            except Exception as enqueue_exc:
                enqueue_error = _safe_error(enqueue_exc)
            _log_standalone(
                "queue_pointer_save_failed_after_send",
                video_id=reservation.video_id,
                actor=actor,
                data={
                    "video_id": reservation.video_id,
                    "generation": reservation.generation,
                    "error": error,
                    "repair_job_id": repair_job_id,
                    "repair_enqueue_error": enqueue_error,
                },
            )
        return QueueDeliveryResult(
            video_id=reservation.video_id,
            sent=True,
            pointer_saved=False,
            message_id=message_id,
            error=error,
            repair_job_id=repair_job_id,
            send_duration_ms=send_duration_ms,
            save_duration_ms=int((time.monotonic() - save_started) * 1000),
        )

    return QueueDeliveryResult(
        video_id=reservation.video_id,
        sent=True,
        pointer_saved=True,
        message_id=message_id,
        send_duration_ms=send_duration_ms,
        save_duration_ms=int((time.monotonic() - save_started) * 1000),
    )


def pump_queue_live(
    tg,
    actor: Any = None,
    *,
    reason: str,
    queue_name: str = ADMIN_QUEUE_NAME,
) -> dict[str, Any]:
    with db.transaction() as conn:
        reservation = reserve_next_pending_card(conn, queue_name=queue_name, reason=reason)
    if reservation is None:
        return {
            "pending_count": 0,
            "global_pending_count": 0,
            "active_video_id": None,
            "active_message_id": None,
            "sent": False,
            "pointer_saved": False,
        }
    delivery = deliver_reserved_card(tg, reservation, actor=actor)
    return {
        "pending_count": reservation.pending_count,
        "global_pending_count": reservation.global_pending_count,
        "active_video_id": reservation.video_id,
        "active_message_id": delivery.message_id or reservation.active_message_id,
        "sent": delivery.sent,
        "pointer_saved": delivery.pointer_saved,
        "error": delivery.error,
        "repair_job_id": delivery.repair_job_id,
        "generation": reservation.generation,
        "reserve_duration_ms": reservation.reserve_duration_ms,
        "send_duration_ms": delivery.send_duration_ms,
        "save_duration_ms": delivery.save_duration_ms,
        "queue_filter_type": reservation.filter_type,
        "queue_filter_value": reservation.filter_value,
    }


def _reservation_from_adoption(payload: dict[str, Any]) -> QueueReservation:
    return QueueReservation(
        queue_name=str(payload.get("queue_name") or ADMIN_QUEUE_NAME),
        video_id=int(payload.get("video_id") or 0),
        chat_id=int(payload.get("chat_id") or 0),
        token=str(payload.get("reservation_token") or ""),
        generation=int(payload.get("generation") or 0),
        reserved_at=datetime.now(timezone.utc),
        delivery_attempts=0,
        reason="adopt_sent_message",
        should_deliver=False,
        active_message_id=int(payload.get("message_id") or 0) or None,
    )


def _adopt_sent_message(conn, payload: dict[str, Any], *, actor: Any = None) -> bool:
    reservation = _reservation_from_adoption(payload)
    if not all(
        (
            reservation.video_id,
            reservation.chat_id,
            reservation.active_message_id,
            reservation.token,
            reservation.generation,
        )
    ):
        return False
    return _save_pointer_message(
        conn,
        reservation,
        chat_id=reservation.chat_id,
        message_id=int(reservation.active_message_id),
        actor=actor,
    )


def _clear_stale_pending_metadata(
    conn,
    *,
    active_video_id: int | None,
    actor: Any = None,
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE videos
            SET admin_message_chat_id = NULL,
                admin_message_id = NULL,
                admin_notified_at = NULL,
                updated_at = now()
            WHERE status = 'pending'
              AND (%s::bigint IS NULL OR id <> %s)
              AND admin_message_id IS NOT NULL
            RETURNING id
            """,
            (active_video_id, active_video_id),
        )
        rows = list(cur.fetchall())
    count = len(rows)
    if count:
        _log_event(
            conn,
            "queue_stale_pending_message_metadata_cleared",
            video_id=active_video_id,
            actor=actor,
            data={"count": count, "active_video_id": active_video_id},
        )
    return count


def _repair_queue_if_needed(
    tg=None,
    *,
    reason: str,
    force: bool = False,
    queue_name: str = ADMIN_QUEUE_NAME,
    adopt_message: dict[str, Any] | None = None,
    actor: Any = None,
) -> QueueRepairResult:
    started = time.monotonic()
    repaired = False
    pump_needed = False
    repair_reason = reason
    active_video_id: int | None = None
    active_message_id: int | None = None
    stale_cleared = 0
    adopted = False
    repair_job_id: int | None = None
    with db.transaction() as conn:
        _log_event(
            conn,
            "queue_repair_started",
            actor=actor,
            data={"reason": reason, "force": force},
        )
        if adopt_message:
            adopted = _adopt_sent_message(conn, adopt_message, actor=actor)
            repaired = adopted
        state = queue_state_for_update(conn, queue_name)
        active_video_id = int(state["active_video_id"]) if state.get("active_video_id") else None
        active_message_id = int(state["active_message_id"]) if state.get("active_message_id") else None
        active_video: dict[str, Any] | None = None
        if active_video_id:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, status, project_code, project_name,
                           admin_message_chat_id, admin_message_id
                    FROM videos
                    WHERE id = %s
                    FOR UPDATE
                    """,
                    (active_video_id,),
                )
                active_video = cur.fetchone()

        if active_video_id and (
            not active_video
            or active_video.get("status") != "pending"
            or not video_matches_filter(active_video, state)
        ):
            repair_reason = "invalid active status or filter"
            _clear_active_fields(conn, queue_name=queue_name, repair_reason=repair_reason)
            active_video_id = None
            active_message_id = None
            repaired = True
            pump_needed = True
        elif active_video_id and active_video:
            if active_message_id:
                if not state.get("active_reservation_token"):
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            UPDATE admin_queue_state
                            SET active_reservation_token = %s,
                                active_reserved_at = COALESCE(active_reserved_at, now()),
                                active_generation = GREATEST(active_generation, 1),
                                active_delivery_attempts = GREATEST(active_delivery_attempts, 1),
                                updated_at = now()
                            WHERE queue_name = %s
                            """,
                            (uuid.uuid4(), queue_name),
                        )
                    repaired = True
                    repair_reason = "active reservation metadata restored"
                metadata_matches = bool(
                    int(active_video.get("admin_message_chat_id") or 0)
                    == int(state.get("active_chat_id") or 0)
                    and int(active_video.get("admin_message_id") or 0) == active_message_id
                )
                if not metadata_matches:
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            UPDATE videos
                            SET admin_message_chat_id = %s,
                                admin_message_id = %s,
                                admin_notified_at = COALESCE(admin_notified_at, now()),
                                updated_at = now()
                            WHERE id = %s AND status = 'pending'
                            """,
                            (state.get("active_chat_id"), active_message_id, active_video_id),
                        )
                    repaired = True
                    repair_reason = "active video metadata synchronized"
            elif not _reservation_is_recent(state):
                pump_needed = True
                repaired = True
                repair_reason = "stale reservation recovered"
        else:
            pending_count, _ = _count_pending(conn, state)
            if pending_count:
                pump_needed = True
                repaired = True
                repair_reason = "missing active pointer recovered"

        stale_cleared = _clear_stale_pending_metadata(
            conn,
            active_video_id=active_video_id,
            actor=actor,
        )
        if stale_cleared:
            repaired = True
            if repair_reason == reason:
                repair_reason = "stale pending metadata cleared"

        if repaired or force:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE admin_queue_state
                    SET last_repaired_at = now(),
                        last_repair_reason = %s,
                        updated_at = now()
                    WHERE queue_name = %s
                    """,
                    (repair_reason, queue_name),
                )
        if pump_needed and tg is None:
            repair_job_id = jobs.enqueue_admin_queue_pump(conn=conn)
        _log_event(
            conn,
            "queue_repair_done",
            video_id=active_video_id,
            actor=actor,
            data={
                "reason": repair_reason,
                "repaired": repaired,
                "adopted_message": adopted,
                "pump_needed": pump_needed,
                "stale_metadata_cleared": stale_cleared,
                "old_active_video_id": active_video_id,
                "new_active_video_id": active_video_id,
                "duration_ms": int((time.monotonic() - started) * 1000),
            },
        )

    pump_result: dict[str, Any] | None = None
    if pump_needed and tg is not None:
        pump_result = pump_queue_live(
            tg,
            actor,
            reason=f"repair:{repair_reason}",
            queue_name=queue_name,
        )
        active_video_id = pump_result.get("active_video_id")
        active_message_id = pump_result.get("active_message_id")
    elif repair_job_id is not None:
        try:
            from bot.worker_kick import kick_worker_if_ready

            kick_worker_if_ready(reason="queue_repair")
        except Exception:
            pass

    return QueueRepairResult(
        repaired=repaired,
        reason=repair_reason,
        active_video_id=active_video_id,
        active_message_id=active_message_id,
        stale_metadata_cleared=stale_cleared,
        pump_needed=pump_needed,
        pump_result=pump_result,
        adopted_message=adopted,
        repair_job_id=repair_job_id,
    )


def repair_queue_if_needed(
    tg=None,
    *,
    reason: str,
    force: bool = False,
    queue_name: str = ADMIN_QUEUE_NAME,
    adopt_message: dict[str, Any] | None = None,
    actor: Any = None,
) -> QueueRepairResult:
    started = time.monotonic()
    try:
        return _repair_queue_if_needed(
            tg,
            reason=reason,
            force=force,
            queue_name=queue_name,
            adopt_message=adopt_message,
            actor=actor,
        )
    except Exception as exc:
        _log_standalone(
            "queue_repair_failed",
            actor=actor,
            data={
                "reason": reason,
                "error": _safe_error(exc),
                "duration_ms": int((time.monotonic() - started) * 1000),
            },
        )
        raise


def _complete_active_action_in_conn(
    conn,
    *,
    callback_chat_id: int,
    callback_message_id: int,
    video_id: int,
    actor: Any,
    action: str,
    mutation: Callable[[Any, dict[str, Any]], dict[str, Any] | None],
    queue_name: str,
) -> CompletionResult:
    started = time.monotonic()
    trace_id = current_trace_id()
    _log_event(
        conn,
        "admin_action_started",
        video_id=video_id,
        actor=actor,
        trace_id=trace_id,
        data={"video_id": video_id, "action": action},
    )
    state = queue_state_for_update(conn, queue_name)
    current_id = int(state["active_video_id"]) if state.get("active_video_id") else None
    generation = int(state.get("active_generation") or 0)
    pointer_matches = bool(
        current_id == video_id
        and int(state.get("active_chat_id") or 0) == int(callback_chat_id)
        and int(state.get("active_message_id") or 0) == int(callback_message_id)
    )
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM videos WHERE id = %s FOR UPDATE", (video_id,))
        locked = cur.fetchone()
    if not pointer_matches or not locked or locked.get("status") != "pending":
        return CompletionResult(
            accepted=False,
            video_id=video_id,
            action=action,
            error="stale queue card",
            old_active_video_id=current_id,
            generation=generation,
            trace_id=trace_id,
            timings_ms={"transaction_ms": int((time.monotonic() - started) * 1000)},
        )
    _log_event(
        conn,
        "admin_action_validated",
        video_id=video_id,
        actor=actor,
        trace_id=trace_id,
        data={
            "video_id": video_id,
            "action": action,
            "old_active_video_id": current_id,
            "generation": generation,
            "duration_ms": int((time.monotonic() - started) * 1000),
        },
    )
    video = mutation(conn, locked)
    _log_event(
        conn,
        "admin_video_mutation_committed",
        video_id=video_id,
        actor=actor,
        trace_id=trace_id,
        data={
            "video_id": video_id,
            "action": action,
            "old_active_video_id": current_id,
            "generation": generation,
            "duration_ms": int((time.monotonic() - started) * 1000),
        },
    )
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE videos
            SET admin_message_chat_id = NULL,
                admin_message_id = NULL,
                admin_notified_at = NULL,
                updated_at = now()
            WHERE id = %s
            """,
            (video_id,),
        )
    _clear_active_fields(conn, queue_name=queue_name, clear_error=True)
    _log_event(
        conn,
        "queue_active_cleared",
        video_id=video_id,
        actor=actor,
        trace_id=trace_id,
        data={
            "video_id": video_id,
            "action": action,
            "old_active_video_id": current_id,
            "new_active_video_id": None,
            "generation": generation,
            "duration_ms": int((time.monotonic() - started) * 1000),
        },
    )
    return CompletionResult(
        accepted=True,
        video_id=video_id,
        action=action,
        video=video,
        old_active_video_id=current_id,
        generation=generation,
        trace_id=trace_id,
        timings_ms={"transaction_ms": int((time.monotonic() - started) * 1000)},
    )


def complete_active_action(
    *,
    callback_chat_id: int,
    callback_message_id: int,
    video_id: int,
    actor: Any,
    action: str,
    mutation: Callable[[Any, dict[str, Any]], dict[str, Any] | None],
    queue_name: str = ADMIN_QUEUE_NAME,
    conn=None,
) -> CompletionResult:
    started = time.monotonic()
    if conn is not None:
        result = _complete_active_action_in_conn(
            conn,
            callback_chat_id=callback_chat_id,
            callback_message_id=callback_message_id,
            video_id=video_id,
            actor=actor,
            action=action,
            mutation=mutation,
            queue_name=queue_name,
        )
        result.timings_ms["commit_ms"] = 0
        return result
    with db.transaction() as owned_conn:
        result = _complete_active_action_in_conn(
            owned_conn,
            callback_chat_id=callback_chat_id,
            callback_message_id=callback_message_id,
            video_id=video_id,
            actor=actor,
            action=action,
            mutation=mutation,
            queue_name=queue_name,
        )
    result.timings_ms["commit_ms"] = int((time.monotonic() - started) * 1000)
    return result


def reset_queue(
    *,
    actor: Any = None,
    queue_name: str = ADMIN_QUEUE_NAME,
) -> dict[str, Any]:
    with db.transaction() as conn:
        state = queue_state_for_update(conn, queue_name)
        active_video_id = int(state["active_video_id"]) if state.get("active_video_id") else None
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, admin_message_chat_id, admin_message_id
                FROM videos
                WHERE status = 'pending'
                  AND admin_message_chat_id IS NOT NULL
                  AND admin_message_id IS NOT NULL
                ORDER BY created_at ASC, id ASC
                """
            )
            old_cards = list(cur.fetchall())
            cur.execute(
                """
                DELETE FROM user_sessions
                WHERE state IN ('admin:date', 'admin:project_other', 'admin:search', 'admin:person')
                """
            )
        _clear_active_fields(
            conn,
            queue_name=queue_name,
            repair_reason="explicit queue reset",
            clear_error=True,
        )
        cleared = _clear_stale_pending_metadata(conn, active_video_id=None, actor=actor)
        _log_event(
            conn,
            "reset",
            video_id=active_video_id,
            actor=actor,
            data={"old_card_count": len(old_cards), "stale_metadata_cleared": cleared},
        )
    return {"old_cards": old_cards, "stale_metadata_cleared": cleared}


def get_queue_diagnostics(queue_name: str = ADMIN_QUEUE_NAME) -> dict[str, Any]:
    with db.connect() as conn:
        state = read_queue_state(conn, queue_name)
        condition, params = queue_filter_sql(state)
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT status, admin_message_chat_id, admin_message_id
                FROM videos
                WHERE id = %s
                """,
                (state.get("active_video_id"),),
            )
            active_video = cur.fetchone() if state.get("active_video_id") else None
            cur.execute(
                f"""
                SELECT id, created_at
                FROM videos v
                WHERE v.status = 'pending' AND {condition}
                ORDER BY v.created_at ASC, v.id ASC
                LIMIT 1
                """,
                params,
            )
            oldest = cur.fetchone()
            cur.execute(
                f"SELECT count(*) AS count FROM videos v "
                f"WHERE v.status = 'pending' AND {condition}",
                params,
            )
            eligible = int(cur.fetchone()["count"])
            cur.execute(
                """
                SELECT count(*) AS count
                FROM videos
                WHERE status = 'pending'
                  AND (%s::bigint IS NULL OR id <> %s)
                  AND admin_message_id IS NOT NULL
                """,
                (state.get("active_video_id"), state.get("active_video_id")),
            )
            stale_count = int(cur.fetchone()["count"])
    reserved_at = _aware(state.get("active_reserved_at"))
    reservation_age = (
        max(0.0, (datetime.now(timezone.utc) - reserved_at).total_seconds())
        if reserved_at
        else None
    )
    return {
        "active_video_id": int(state["active_video_id"]) if state.get("active_video_id") else None,
        "active_status": active_video.get("status") if active_video else None,
        "active_message_id": int(state["active_message_id"])
        if state.get("active_message_id")
        else None,
        "active_chat_id": int(state["active_chat_id"]) if state.get("active_chat_id") else None,
        "generation": int(state.get("active_generation") or 0),
        "reservation_age_seconds": round(reservation_age, 3)
        if reservation_age is not None
        else None,
        "delivery_attempts": int(state.get("active_delivery_attempts") or 0),
        "oldest_pending_video_id": int(oldest["id"]) if oldest else None,
        "eligible_pending": eligible,
        "stale_pending_message_ids": stale_count,
        "last_error": state.get("active_last_error"),
        "last_error_at": state.get("active_last_error_at"),
        "last_repaired_at": state.get("last_repaired_at"),
        "last_repair_reason": state.get("last_repair_reason"),
        "filter_type": queue_filter(state)[0],
        "filter_value": queue_filter(state)[1],
    }


class _AcceptanceTelegram:
    def __init__(self) -> None:
        self.next_message_id = 900_000
        self.sent_video_ids: list[int] = []

    def send_message(self, _chat_id: int, _text: str, reply_markup=None) -> dict[str, Any]:
        self.next_message_id += 1
        try:
            callback_data = reply_markup["inline_keyboard"][0][0]["callback_data"]
            self.sent_video_ids.append(int(str(callback_data).split(":")[-1]))
        except Exception:
            pass
        return {"ok": True, "result": {"message_id": self.next_message_id}}


def run_isolated_acceptance(*, actions: int = 10) -> dict[str, Any]:
    if actions < 1:
        raise ValueError("actions must be positive")
    queue_name = f"acceptance-{uuid.uuid4()}"
    project_code = f"__acceptance_{uuid.uuid4().hex}__"
    actor = SimpleNamespace(tg_id=0, username="acceptance")
    tg = _AcceptanceTelegram()
    latencies: list[dict[str, int]] = []
    fixture_ids: list[int] = []
    final_snapshot: dict[str, Any] = {}
    statuses = ("approved", "duplicate", "needs_revision")
    cleanup_complete = False
    try:
        with db.transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO admin_queue_state (
                        queue_name, queue_filter_type, queue_filter_value
                    )
                    VALUES (%s, 'project', %s)
                    """,
                    (queue_name, project_code),
                )
                for index in range(actions + 1):
                    unique_id = uuid.uuid4().hex
                    cur.execute(
                        """
                        INSERT INTO videos (
                            status, project_code, project_name, publish_date,
                            instagram_url, instagram_id, author_name, montage_name,
                            added_by_tg_id, added_by_username, created_at
                        )
                        VALUES (
                            'pending', %s, 'Acceptance fixture', %s,
                            %s, %s, 'Fixture author', 'Fixture montage',
                            0, 'acceptance', now() + (%s * interval '1 millisecond')
                        )
                        RETURNING id
                        """,
                        (
                            project_code,
                            date.today(),
                            f"https://example.invalid/{unique_id}",
                            f"acceptance-{unique_id}",
                            index,
                        ),
                    )
                    fixture_ids.append(int(cur.fetchone()["id"]))

        initial = pump_queue_live(
            tg,
            actor,
            reason="isolated_acceptance_initial",
            queue_name=queue_name,
        )
        if not initial.get("pointer_saved") or not initial.get("active_message_id"):
            raise RuntimeError("acceptance queue did not save its first pointer")

        current_video_id = int(initial["active_video_id"])
        current_message_id = int(initial["active_message_id"])
        for index in range(actions):
            action_started = time.monotonic()
            target_status = statuses[index % len(statuses)]

            def mutate(test_conn, _locked, status=target_status, target_id=current_video_id):
                with test_conn.cursor() as mutation_cur:
                    mutation_cur.execute(
                        """
                        UPDATE videos
                        SET status = %s,
                            checked_at = now(),
                            checked_by_tg_id = 0,
                            checked_by_username = 'acceptance',
                            updated_at = now()
                        WHERE id = %s AND status = 'pending'
                        RETURNING *
                        """,
                        (status, target_id),
                    )
                    return mutation_cur.fetchone()

            completion = complete_active_action(
                callback_chat_id=int(get_settings().admin_chat_id),
                callback_message_id=current_message_id,
                video_id=current_video_id,
                actor=actor,
                action=target_status,
                mutation=mutate,
                queue_name=queue_name,
            )
            if not completion.accepted:
                raise RuntimeError(f"acceptance action {index + 1} was rejected")
            next_result = pump_queue_live(
                tg,
                actor,
                reason=f"isolated_acceptance_after_{index + 1}",
                queue_name=queue_name,
            )
            if not next_result.get("pointer_saved") or not next_result.get("active_message_id"):
                raise RuntimeError(f"acceptance action {index + 1} did not save next pointer")
            with db.connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT q.active_video_id, q.active_message_id, v.status,
                               (
                                   SELECT id FROM videos
                                   WHERE status = 'pending' AND project_code = %s
                                   ORDER BY created_at, id LIMIT 1
                               ) AS oldest_id,
                               (
                                   SELECT count(*) FROM videos
                                   WHERE status = 'pending'
                                     AND project_code = %s
                                     AND id <> q.active_video_id
                                     AND admin_message_id IS NOT NULL
                               ) AS stale_count
                        FROM admin_queue_state q
                        LEFT JOIN videos v ON v.id = q.active_video_id
                        WHERE q.queue_name = %s
                        """,
                        (project_code, project_code, queue_name),
                    )
                    snapshot = cur.fetchone()
            if (
                not snapshot
                or snapshot.get("status") != "pending"
                or not snapshot.get("active_message_id")
                or int(snapshot.get("active_video_id") or 0)
                != int(snapshot.get("oldest_id") or 0)
                or int(snapshot.get("stale_count") or 0) != 0
            ):
                raise RuntimeError(f"acceptance invariant failed after action {index + 1}")
            latencies.append(
                {
                    "action": index + 1,
                    "callback_received_ms": 0,
                    "mutation_commit_ms": int(completion.timings_ms.get("commit_ms") or 0),
                    "next_reservation_ms": int(next_result.get("reserve_duration_ms") or 0),
                    "card_send_ms": int(next_result.get("send_duration_ms") or 0),
                    "pointer_save_ms": int(next_result.get("save_duration_ms") or 0),
                    "total_ms": int((time.monotonic() - action_started) * 1000),
                }
            )
            final_snapshot = dict(snapshot)
            current_video_id = int(snapshot["active_video_id"])
            current_message_id = int(snapshot["active_message_id"])
    finally:
        if fixture_ids:
            with db.transaction() as cleanup_conn:
                with cleanup_conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM admin_queue_state WHERE queue_name = %s",
                        (queue_name,),
                    )
                    cur.execute(
                        "DELETE FROM logs WHERE entity_type = 'admin_queue' AND entity_id = ANY(%s)",
                        (fixture_ids,),
                    )
                    cur.execute("DELETE FROM videos WHERE id = ANY(%s)", (fixture_ids,))
            cleanup_complete = True

    duplicate_cards = len(tg.sent_video_ids) - len(set(tg.sent_video_ids))
    if duplicate_cards:
        raise RuntimeError(f"acceptance sent {duplicate_cards} duplicate cards")
    return {
        "ok": True,
        "isolated": True,
        "committed_transactions": True,
        "cleaned_up": cleanup_complete,
        "actions": actions,
        "advanced": len(latencies),
        "manual_repair_commands": 0,
        "duplicate_cards": duplicate_cards,
        "stale_callbacks": 0,
        "sent_cards": len(tg.sent_video_ids),
        "final_active_fixture_id": final_snapshot.get("active_video_id"),
        "final_active_status": final_snapshot.get("status"),
        "final_active_message_id": final_snapshot.get("active_message_id"),
        "final_fifo_head_fixture_id": final_snapshot.get("oldest_id"),
        "latencies_ms": latencies,
    }
