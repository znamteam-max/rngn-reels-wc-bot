from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

from bot import db, jobs, metrics, sheets
from bot.config import get_settings
from bot.telegram import TelegramAPIError, TelegramClient, inline_keyboard


RETRY_DELAYS_SECONDS = (10, 30, 120, 300, 900)
MAX_TELEGRAM_SENDS = 10
MAX_SHEETS_VIDEO_SYNCS = 10
BULK_CHUNK_SIZE = 10


class PermanentJobError(RuntimeError):
    pass


class SuspendedJobError(RuntimeError):
    pass


@dataclass
class WorkerContext:
    invocation_id: str
    started_monotonic: float
    time_budget_seconds: int
    telegram_sends: int = 0
    sheets_video_syncs: int = 0
    _telegram: TelegramClient | None = None
    _sheets_service: Any = None

    def telegram(self) -> TelegramClient:
        if self._telegram is None:
            self._telegram = TelegramClient()
        return self._telegram

    def sheets_service(self):
        if self._sheets_service is None:
            self._sheets_service = sheets._service()
        return self._sheets_service

    def elapsed(self) -> float:
        return time.monotonic() - self.started_monotonic


def _safe_error(exc: Exception) -> str:
    text = f"{type(exc).__name__}: {exc}"
    settings = get_settings()
    for secret in (settings.bot_token, settings.database_url, settings.google_service_account_json_b64):
        if secret:
            text = text.replace(secret, "[secret]")
    return text[:500]


def recover_stale_jobs() -> dict[str, int]:
    recovered = 0
    dead = 0
    with db.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, attempts, max_attempts
                FROM background_jobs
                WHERE status = 'processing'
                  AND locked_at < now() - interval '5 minutes'
                FOR UPDATE SKIP LOCKED
                """
            )
            rows = list(cur.fetchall())
            for row in rows:
                is_dead = int(row["attempts"]) >= int(row["max_attempts"])
                cur.execute(
                    """
                    UPDATE background_jobs
                    SET status = %s,
                        available_at = CASE WHEN %s THEN available_at ELSE now() END,
                        locked_at = NULL,
                        locked_by = NULL,
                        started_at = NULL,
                        finished_at = CASE WHEN %s THEN now() ELSE finished_at END,
                        last_error = CASE
                            WHEN %s THEN COALESCE(last_error, 'stale processing exhausted attempts')
                            ELSE last_error
                        END,
                        updated_at = now()
                    WHERE id = %s
                    """,
                    ("dead" if is_dead else "queued", is_dead, is_dead, is_dead, row["id"]),
                )
                action = "job_dead" if is_dead else "job_recovered_stale"
                db.log_event(
                    conn,
                    entity_type="background_job",
                    entity_id=int(row["id"]),
                    action=action,
                    after_data={"attempts": int(row["attempts"])},
                )
                dead += int(is_dead)
                recovered += int(not is_dead)
    return {"recovered": recovered, "dead": dead}


def claim_jobs(limit: int, invocation_id: str) -> list[dict[str, Any]]:
    with db.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT *
                FROM background_jobs
                WHERE status = 'queued'
                  AND available_at <= now()
                ORDER BY priority ASC, available_at ASC, id ASC
                FOR UPDATE SKIP LOCKED
                LIMIT %s
                """,
                (max(1, min(20, int(limit))),),
            )
            rows = list(cur.fetchall())
            if not rows:
                return []
            ids = [int(row["id"]) for row in rows]
            cur.execute(
                """
                UPDATE background_jobs
                SET status = 'processing',
                    attempts = attempts + 1,
                    locked_at = now(),
                    locked_by = %s,
                    started_at = now(),
                    updated_at = now()
                WHERE id = ANY(%s)
                """,
                (invocation_id, ids),
            )
        for row in rows:
            row["attempts"] = int(row.get("attempts") or 0) + 1
            db.log_event(
                conn,
                entity_type="background_job",
                entity_id=int(row["id"]),
                action="job_claimed",
                after_data={"kind": row["kind"], "attempt": row["attempts"], "worker": invocation_id},
            )
    return rows


def _finish_job(job: dict[str, Any], duration_ms: int) -> None:
    with db.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE background_jobs
                SET status = 'done',
                    finished_at = now(),
                    locked_at = NULL,
                    locked_by = NULL,
                    last_error = NULL,
                    updated_at = now()
                WHERE id = %s AND status = 'processing'
                """,
                (job["id"],),
            )
        db.log_event(
            conn,
            entity_type="background_job",
            entity_id=int(job["id"]),
            action="job_done",
            after_data={"kind": job["kind"], "duration_ms": duration_ms},
        )


def _terminal_side_effect(conn, job: dict[str, Any], error: str) -> None:
    payload = job.get("payload") or {}
    if job["kind"] == "sheets_sync_video" and payload.get("video_id"):
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE videos
                SET sheet_sync_status = 'failed',
                    sheet_sync_error = %s,
                    updated_at = now()
                WHERE id = %s
                """,
                (error[:300], int(payload["video_id"])),
            )
    if job["kind"] == "telegram_notify" and payload.get("operation_id"):
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE bulk_operations
                SET failure_count = failure_count + 1,
                    updated_at = now()
                WHERE id = %s
                """,
                (int(payload["operation_id"]),),
            )


def _fail_job(job: dict[str, Any], exc: Exception) -> str:
    error = _safe_error(exc)
    attempts = int(job.get("attempts") or 1)
    max_attempts = int(job.get("max_attempts") or 8)
    permanent = isinstance(exc, PermanentJobError)
    suspended = isinstance(exc, SuspendedJobError)
    exhausted = attempts >= max_attempts
    retry_after = exc.retry_after if isinstance(exc, TelegramAPIError) else None
    if permanent or exhausted:
        next_status = "dead"
        action = "job_dead"
        available_at = None
    elif suspended:
        next_status = "failed"
        action = "job_retry"
        available_at = None
    else:
        next_status = "queued"
        action = "job_retry"
        delay = retry_after or RETRY_DELAYS_SECONDS[min(attempts - 1, len(RETRY_DELAYS_SECONDS) - 1)]
        available_at = datetime.now(timezone.utc) + timedelta(seconds=int(delay))

    with db.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE background_jobs
                SET status = %s,
                    available_at = COALESCE(%s, available_at),
                    locked_at = NULL,
                    locked_by = NULL,
                    finished_at = CASE WHEN %s IN ('dead', 'failed') THEN now() ELSE NULL END,
                    last_error = %s,
                    updated_at = now()
                WHERE id = %s
                """,
                (next_status, available_at, next_status, error, job["id"]),
            )
            if job["kind"] == "sheets_sync_video" and (job.get("payload") or {}).get("video_id"):
                cur.execute(
                    """
                    UPDATE videos
                    SET sheet_sync_status = CASE WHEN %s = 'dead' THEN 'failed' ELSE 'queued' END,
                        sheet_sync_error = %s,
                        updated_at = now()
                    WHERE id = %s
                    """,
                    (next_status, error[:300], int(job["payload"]["video_id"])),
                )
        if next_status == "dead":
            _terminal_side_effect(conn, job, error)
        db.log_event(
            conn,
            entity_type="background_job",
            entity_id=int(job["id"]),
            action=action,
            after_data={
                "kind": job["kind"],
                "status": next_status,
                "attempt": attempts,
                "retry_after": retry_after,
                "error": error,
            },
        )
        if job["kind"] == "sheets_sync_video":
            db.log_event(
                conn,
                entity_type="video",
                entity_id=int((job.get("payload") or {}).get("video_id") or 0) or None,
                action="sheets_sync_failed",
                after_data={"job_id": int(job["id"]), "status": next_status, "error": error},
            )
    return next_status


def _release_unprocessed(jobs_to_release: list[dict[str, Any]]) -> None:
    if not jobs_to_release:
        return
    ids = [int(job["id"]) for job in jobs_to_release]
    db.execute(
        """
        UPDATE background_jobs
        SET status = 'queued',
            attempts = GREATEST(attempts - 1, 0),
            locked_at = NULL,
            locked_by = NULL,
            started_at = NULL,
            updated_at = now()
        WHERE id = ANY(%s) AND status = 'processing'
        """,
        (ids,),
    )


def _handle_dashboard_refresh(payload: dict[str, Any], context: WorkerContext) -> None:
    from bot import handlers as h

    h.refresh_admin_dashboard(context.telegram())
    context.telegram_sends += 1
    h.record_system_log(
        "dashboard_refresh_done",
        "admin_dashboard",
        None,
        {"worker": context.invocation_id},
    )


def _handle_queue_pump(payload: dict[str, Any], context: WorkerContext) -> None:
    from bot import handlers as h

    h.pump_admin_queue(
        context.telegram(),
        force_repost=bool(payload.get("force_repost")),
    )
    context.telegram_sends += 1


def _handle_sheets_video(payload: dict[str, Any], context: WorkerContext) -> None:
    from bot import handlers as h

    video_id = int(payload.get("video_id") or 0)
    if not video_id:
        raise PermanentJobError("sheets_sync_video requires video_id")
    video = h.get_video_by_id_outside(video_id)
    if not video:
        raise PermanentJobError(f"video {video_id} not found")
    db.execute(
        """
        UPDATE videos
        SET sheet_sync_status = 'syncing',
            sheet_sync_attempts = sheet_sync_attempts + 1,
            updated_at = now()
        WHERE id = %s
        """,
        (video_id,),
    )
    row_number = sheets.upsert_video(video, service=context.sheets_service())
    db.execute(
        """
        UPDATE videos
        SET sheet_row = COALESCE(%s, sheet_row),
            sheet_sync_status = 'synced',
            sheet_sync_error = NULL,
            sheet_synced_at = now(),
            updated_at = now()
        WHERE id = %s
        """,
        (row_number or None, video_id),
    )
    jobs.enqueue_job(
        "sheets_sync_stats",
        {},
        dedupe_key="stats:projects",
        priority=80,
    )
    context.sheets_video_syncs += 1
    h.record_system_log(
        "sheets_sync_done",
        "video",
        video_id,
        {"row_number": row_number},
    )


def _handle_sheets_video_batch(
    job_batch: list[dict[str, Any]],
    context: WorkerContext,
) -> dict[int, Exception]:
    from bot import handlers as h

    errors: dict[int, Exception] = {}
    ids_by_job: dict[int, int] = {}
    for job in job_batch:
        video_id = int((job.get("payload") or {}).get("video_id") or 0)
        if not video_id:
            errors[int(job["id"])] = PermanentJobError("sheets_sync_video requires video_id")
        else:
            ids_by_job[int(job["id"])] = video_id
    if not ids_by_job:
        return errors

    rows = db.fetch_all(
        h.VIDEO_SELECT + " WHERE v.id = ANY(%s)",
        (list(ids_by_job.values()),),
    )
    videos_by_id = {int(row["id"]): row for row in rows}
    runnable: list[dict[str, Any]] = []
    runnable_jobs: list[dict[str, Any]] = []
    for job in job_batch:
        video_id = ids_by_job.get(int(job["id"]))
        if not video_id:
            continue
        video = videos_by_id.get(video_id)
        if not video:
            errors[int(job["id"])] = PermanentJobError(f"video {video_id} not found")
            continue
        runnable_jobs.append(job)
        runnable.append(video)
    if not runnable:
        return errors

    db.execute(
        """
        UPDATE videos
        SET sheet_sync_status = 'syncing',
            sheet_sync_attempts = sheet_sync_attempts + 1,
            updated_at = now()
        WHERE id = ANY(%s)
        """,
        ([int(video["id"]) for video in runnable],),
    )
    context.sheets_video_syncs += len(runnable)
    try:
        row_numbers = sheets.batch_upsert_videos(runnable, service=context.sheets_service())
    except Exception as exc:
        for job in runnable_jobs:
            errors[int(job["id"])] = exc
        return errors

    with db.transaction() as conn:
        with conn.cursor() as cur:
            for video in runnable:
                video_id = int(video["id"])
                cur.execute(
                    """
                    UPDATE videos
                    SET sheet_row = COALESCE(%s, sheet_row),
                        sheet_sync_status = 'synced',
                        sheet_sync_error = NULL,
                        sheet_synced_at = now(),
                        updated_at = now()
                    WHERE id = %s
                    """,
                    (row_numbers.get(video_id) or None, video_id),
                )
                db.log_event(
                    conn,
                    entity_type="video",
                    entity_id=video_id,
                    action="sheets_sync_done",
                    after_data={"row_number": row_numbers.get(video_id)},
                )
        jobs.enqueue_job(
            "sheets_sync_stats",
            {},
            dedupe_key="stats:projects",
            priority=80,
            conn=conn,
        )
    return errors


def _handle_sheets_stats(payload: dict[str, Any], context: WorkerContext) -> None:
    from bot import handlers as h

    rows = db.fetch_all(h.VIDEO_SELECT + " WHERE v.status <> 'deleted'")
    sheets.sync_project_reports(rows, service=context.sheets_service())


def _handle_telegram_notify(payload: dict[str, Any], context: WorkerContext) -> None:
    operation_id = payload.get("bulk_summary_operation_id")
    if operation_id:
        operation = db.fetch_one("SELECT * FROM bulk_operations WHERE id = %s", (int(operation_id),))
        if not operation:
            raise PermanentJobError(f"bulk operation {operation_id} not found")
        text = (
            f"Операция #{operation_id} завершена.\n"
            f"Возвращено: {operation.get('processed_count', 0)}\n"
            f"Не удалось уведомить: {operation.get('failure_count', 0)}"
        )
    else:
        text = str(payload.get("text") or "")
    chat_id = int(payload.get("chat_id") or 0)
    if not chat_id or not text:
        raise PermanentJobError("telegram_notify requires chat_id and text")
    context.telegram().send_message(chat_id, text, payload.get("reply_markup"))
    context.telegram_sends += 1
    if payload.get("operation_id"):
        db.execute(
            "UPDATE bulk_operations SET success_count = success_count + 1, updated_at = now() WHERE id = %s",
            (int(payload["operation_id"]),),
        )


def _handle_archive_cards(payload: dict[str, Any], context: WorkerContext) -> None:
    from bot import handlers as h

    cards = payload.get("cards")
    if not isinstance(cards, list):
        raise PermanentJobError("archive_admin_cards requires cards")
    for card in cards[:10]:
        h._archive_queue_message(
            context.telegram(),
            int(card.get("chat_id") or 0),
            int(card.get("message_id") or 0),
            str(card.get("text") or "Карточка архивирована."),
        )
        context.telegram_sends += 1


def _handle_daily_report(payload: dict[str, Any], context: WorkerContext) -> None:
    from bot.daily_reports import send_daily_report

    raw_date = payload.get("report_date")
    report_date = date.fromisoformat(str(raw_date)) if raw_date else None
    send_daily_report(
        report_date,
        tg=context.telegram(),
        actor_tg_id=payload.get("actor_tg_id"),
        actor_username=payload.get("actor_username"),
    )
    context.telegram_sends += 1


def _handle_youtube_metrics(payload: dict[str, Any], context: WorkerContext) -> None:
    metrics.sync_youtube_metrics(
        actor_tg_id=payload.get("actor_tg_id"),
        actor_username=payload.get("actor_username"),
    )


def _handle_bulk_return(payload: dict[str, Any], context: WorkerContext) -> None:
    from bot import handlers as h

    operation_id = int(payload.get("operation_id") or 0)
    if not operation_id:
        raise PermanentJobError("bulk_return_missing_dates requires operation_id")
    archive_card: dict[str, Any] | None = None
    with db.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM bulk_operations WHERE id = %s FOR UPDATE", (operation_id,))
            operation = cur.fetchone()
            if not operation:
                raise PermanentJobError(f"bulk operation {operation_id} not found")
            if operation["status"] == "done":
                return
            cur.execute(
                """
                UPDATE bulk_operations
                SET status = 'processing',
                    started_at = COALESCE(started_at, now()),
                    updated_at = now()
                WHERE id = %s
                """,
                (operation_id,),
            )
            cur.execute(
                """
                SELECT id, batch_id, added_by_tg_id, project_name, instagram_url, youtube_url
                FROM videos
                WHERE status = 'pending'
                  AND publish_date IS NULL
                  AND created_at <= %s
                ORDER BY id ASC
                FOR UPDATE SKIP LOCKED
                LIMIT %s
                """,
                (operation["created_at"], BULK_CHUNK_SIZE),
            )
            rows = list(cur.fetchall())
            ids = [int(row["id"]) for row in rows]
            if ids:
                cur.execute(
                    """
                    UPDATE videos
                    SET status = 'needs_revision',
                        checked_by_tg_id = %s,
                        checked_by_username = %s,
                        checked_at = now(),
                        updated_at = now()
                    WHERE id = ANY(%s)
                    """,
                    (operation["created_by_tg_id"], operation["created_by_username"], ids),
                )
            failure_count = 0
            for row in rows:
                db.log_event(
                    conn,
                    entity_type="video",
                    entity_id=int(row["id"]),
                    action="missing_date_returned",
                    actor_tg_id=operation["created_by_tg_id"],
                    actor_username=operation["created_by_username"],
                    before_data={"status": "pending", "publish_date": None},
                    after_data={"status": "needs_revision", "bulk_operation_id": operation_id},
                )
                if row.get("added_by_tg_id"):
                    jobs.enqueue_telegram_notification(
                        int(row["added_by_tg_id"]),
                        h._missing_date_notification_text(row),
                        event_key=f"missing-date:{row['id']}:{row['added_by_tg_id']}",
                        reply_markup=inline_keyboard([[("Указать дату", f"revdate:{row['id']}")]]),
                        operation_id=operation_id,
                        conn=conn,
                    )
                else:
                    failure_count += 1
            for batch_id in sorted({int(row["batch_id"]) for row in rows if row.get("batch_id")}):
                h.recalculate_batch(conn, batch_id)

            state = h._queue_state_for_update(conn)
            active_id = int(state["active_video_id"]) if state.get("active_video_id") else None
            if active_id and active_id in ids:
                if state.get("active_chat_id") and state.get("active_message_id"):
                    archive_card = {
                        "chat_id": int(state["active_chat_id"]),
                        "message_id": int(state["active_message_id"]),
                        "text": f"Заявка #{active_id} возвращена автору на заполнение даты.",
                    }
                h._clear_queue_state(conn)
                jobs.enqueue_admin_queue_pump(conn=conn)

            processed_after = int(operation["processed_count"]) + len(rows)
            cur.execute(
                """
                SELECT count(*) AS count
                FROM videos
                WHERE status = 'pending'
                  AND publish_date IS NULL
                  AND created_at <= %s
                """,
                (operation["created_at"],),
            )
            remaining = int(cur.fetchone()["count"])
            done = remaining == 0 or not rows
            cur.execute(
                """
                UPDATE bulk_operations
                SET status = %s,
                    processed_count = %s,
                    failure_count = failure_count + %s,
                    last_video_id = COALESCE(%s, last_video_id),
                    finished_at = CASE WHEN %s THEN now() ELSE NULL END,
                    updated_at = now()
                WHERE id = %s
                """,
                (
                    "done" if done else "processing",
                    processed_after,
                    failure_count,
                    max(ids) if ids else None,
                    done,
                    operation_id,
                ),
            )
            jobs.enqueue_dashboard_refresh(conn=conn)
            if archive_card:
                jobs.enqueue_job(
                    "archive_admin_cards",
                    {"cards": [archive_card]},
                    dedupe_key=f"archive:bulk:{operation_id}:{active_id}",
                    priority=15,
                    conn=conn,
                )
            if done:
                summary_at = datetime.now(timezone.utc) + timedelta(minutes=5)
                jobs.enqueue_job(
                    "telegram_notify",
                    {
                        "chat_id": int(get_settings().admin_chat_id),
                        "event_key": f"bulk-summary:{operation_id}",
                        "bulk_summary_operation_id": operation_id,
                    },
                    dedupe_key=f"telegram:bulk-summary:{operation_id}",
                    priority=90,
                    available_at=summary_at,
                    conn=conn,
                )
                action = "bulk_operation_done"
            else:
                jobs.enqueue_job(
                    "bulk_return_missing_dates",
                    {"operation_id": operation_id},
                    dedupe_key=f"bulk:{operation_id}:chunk:{processed_after}",
                    priority=30,
                    conn=conn,
                )
                action = "bulk_operation_progress"
            db.log_event(
                conn,
                entity_type="bulk_operation",
                entity_id=operation_id,
                action=action,
                actor_tg_id=operation["created_by_tg_id"],
                actor_username=operation["created_by_username"],
                after_data={"processed": processed_after, "remaining": remaining, "chunk": len(rows)},
            )


JOB_HANDLERS: dict[str, Callable[[dict[str, Any], WorkerContext], None]] = {
    "dashboard_refresh": _handle_dashboard_refresh,
    "sheets_sync_video": _handle_sheets_video,
    "sheets_sync_stats": _handle_sheets_stats,
    "telegram_notify": _handle_telegram_notify,
    "admin_queue_pump": _handle_queue_pump,
    "bulk_return_missing_dates": _handle_bulk_return,
    "archive_admin_cards": _handle_archive_cards,
    "daily_report": _handle_daily_report,
    "youtube_metrics": _handle_youtube_metrics,
}


def _job_fits_budget(job: dict[str, Any], context: WorkerContext) -> bool:
    kind = str(job.get("kind") or "")
    if context.elapsed() >= context.time_budget_seconds:
        return False
    telegram_cost = 1 if kind in {
        "dashboard_refresh",
        "telegram_notify",
        "admin_queue_pump",
        "daily_report",
    } else 0
    if kind == "archive_admin_cards":
        telegram_cost = min(10, len((job.get("payload") or {}).get("cards") or []))
    if context.telegram_sends + telegram_cost > MAX_TELEGRAM_SENDS:
        return False
    if kind == "sheets_sync_video" and context.sheets_video_syncs >= MAX_SHEETS_VIDEO_SYNCS:
        return False
    return True


def process_jobs() -> dict[str, Any]:
    settings = get_settings()
    started = time.monotonic()
    invocation_id = str(uuid.uuid4())
    stale = recover_stale_jobs()
    claimed_jobs = claim_jobs(settings.job_worker_batch_size, invocation_id)
    context = WorkerContext(
        invocation_id=invocation_id,
        started_monotonic=started,
        time_budget_seconds=settings.job_worker_time_budget_seconds,
    )
    done = 0
    retried = 0
    dead = stale["dead"]
    failed = 0
    processed_count = 0
    index = 0
    while index < len(claimed_jobs):
        job = claimed_jobs[index]
        if not _job_fits_budget(job, context):
            _release_unprocessed(claimed_jobs[index:])
            break
        if job.get("kind") == "sheets_sync_video":
            capacity = MAX_SHEETS_VIDEO_SYNCS - context.sheets_video_syncs
            batch: list[dict[str, Any]] = []
            while (
                index + len(batch) < len(claimed_jobs)
                and claimed_jobs[index + len(batch)].get("kind") == "sheets_sync_video"
                and len(batch) < capacity
            ):
                batch.append(claimed_jobs[index + len(batch)])
            if not batch:
                _release_unprocessed(claimed_jobs[index:])
                break
            processed_count += len(batch)
            job_started = time.monotonic()
            batch_errors = _handle_sheets_video_batch(batch, context)
            duration_ms = int((time.monotonic() - job_started) * 1000)
            for sheet_job in batch:
                error = batch_errors.get(int(sheet_job["id"]))
                if error is None:
                    _finish_job(sheet_job, duration_ms)
                    done += 1
                    continue
                status = _fail_job(sheet_job, error)
                retried += int(status == "queued")
                dead += int(status == "dead")
                failed += int(status == "failed")
            index += len(batch)
            continue
        processed_count += 1
        job_started = time.monotonic()
        handler = JOB_HANDLERS.get(str(job.get("kind") or ""))
        if not handler:
            status = _fail_job(job, PermanentJobError(f"unsupported kind: {job.get('kind')}"))
            dead += int(status == "dead")
            failed += int(status == "failed")
            index += 1
            continue
        try:
            handler(job.get("payload") or {}, context)
            _finish_job(job, int((time.monotonic() - job_started) * 1000))
            done += 1
        except Exception as exc:
            status = _fail_job(job, exc)
            retried += int(status == "queued")
            dead += int(status == "dead")
            failed += int(status == "failed")
        index += 1

    ready = db.fetch_one(
        "SELECT count(*) AS count FROM background_jobs WHERE status = 'queued' AND available_at <= now()"
    ) or {}
    return {
        "ok": True,
        "claimed": len(claimed_jobs),
        "processed": processed_count,
        "done": done,
        "retried": retried,
        "failed": failed,
        "dead": dead,
        "remaining_ready": int(ready.get("count") or 0),
        "duration_ms": int((time.monotonic() - started) * 1000),
        "invocation_id": invocation_id,
    }
