from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from bot import db
from bot.config import get_settings


ALLOWED_JOB_KINDS = {
    "dashboard_refresh",
    "sheets_sync_video",
    "sheets_sync_stats",
    "telegram_notify",
    "admin_queue_pump",
    "bulk_return_missing_dates",
    "archive_admin_cards",
    "daily_report",
    "youtube_metrics",
}


def background_jobs_enabled() -> bool:
    return get_settings().background_jobs_enabled


def enqueue_job(
    kind: str,
    payload: dict[str, Any] | None = None,
    *,
    dedupe_key: str | None = None,
    priority: int = 100,
    available_at: datetime | None = None,
    max_attempts: int = 8,
    conn=None,
) -> int | None:
    if kind not in ALLOWED_JOB_KINDS:
        raise ValueError(f"Unsupported background job kind: {kind}")
    if not background_jobs_enabled():
        return None
    if conn is None:
        with db.transaction() as owned_conn:
            return enqueue_job(
                kind,
                payload,
                dedupe_key=dedupe_key,
                priority=priority,
                available_at=available_at,
                max_attempts=max_attempts,
                conn=owned_conn,
            )

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO background_jobs (
                kind, dedupe_key, payload, priority, max_attempts, available_at
            )
            VALUES (%s, %s, %s, %s, %s, COALESCE(%s, now()))
            ON CONFLICT (dedupe_key)
            WHERE dedupe_key IS NOT NULL
              AND status IN ('queued', 'processing')
            DO UPDATE SET
                priority = LEAST(background_jobs.priority, EXCLUDED.priority),
                available_at = LEAST(background_jobs.available_at, EXCLUDED.available_at),
                payload = CASE
                    WHEN background_jobs.status = 'queued' THEN EXCLUDED.payload
                    ELSE background_jobs.payload
                END,
                updated_at = now()
            RETURNING id, (xmax = 0) AS inserted
            """,
            (
                kind,
                dedupe_key,
                Jsonb(payload or {}),
                int(priority),
                max(1, int(max_attempts)),
                available_at,
            ),
        )
        row = cur.fetchone()
    if not row:
        return None
    job_id = int(row["id"])
    if bool(row.get("inserted")):
        db.log_event(
            conn,
            entity_type="background_job",
            entity_id=job_id,
            action="job_enqueued",
            after_data={"kind": kind, "dedupe_key": dedupe_key, "priority": int(priority)},
        )
        if kind == "dashboard_refresh":
            db.log_event(
                conn,
                entity_type="admin_dashboard",
                entity_id=None,
                action="dashboard_refresh_queued",
                after_data={"job_id": job_id},
            )
    return job_id


def enqueue_dashboard_refresh(*, conn=None) -> int | None:
    return enqueue_job(
        "dashboard_refresh",
        {},
        dedupe_key="dashboard:main",
        priority=20,
        conn=conn,
    )


def enqueue_admin_queue_pump(*, conn=None, force_repost: bool = False) -> int | None:
    return enqueue_job(
        "admin_queue_pump",
        {"force_repost": bool(force_repost)},
        dedupe_key="queue:pump:main",
        priority=10,
        conn=conn,
    )


def enqueue_sheet_sync(video_id: int, *, version: str | None = None, conn=None) -> int | None:
    return enqueue_job(
        "sheets_sync_video",
        {"video_id": int(video_id), "version": version or "current"},
        dedupe_key=f"sheets:video:{int(video_id)}",
        priority=60,
        conn=conn,
    )


def enqueue_telegram_notification(
    chat_id: int,
    text: str,
    *,
    event_key: str,
    reply_markup: dict[str, Any] | None = None,
    operation_id: int | None = None,
    available_at: datetime | None = None,
    priority: int = 80,
    conn=None,
) -> int | None:
    payload: dict[str, Any] = {
        "chat_id": int(chat_id),
        "text": str(text),
        "event_key": event_key,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    if operation_id is not None:
        payload["operation_id"] = int(operation_id)
    return enqueue_job(
        "telegram_notify",
        payload,
        dedupe_key=f"telegram:{event_key}",
        priority=priority,
        available_at=available_at,
        conn=conn,
    )


def _telegram_update_meta(update: dict[str, Any]) -> tuple[str, int | None, int | None]:
    if isinstance(update.get("callback_query"), dict):
        callback = update["callback_query"]
        user = callback.get("from") or {}
        chat = (callback.get("message") or {}).get("chat") or {}
        return "callback_query", user.get("id"), chat.get("id")
    message = update.get("message") or update.get("edited_message") or {}
    if isinstance(message, dict) and message:
        user = message.get("from") or {}
        chat = message.get("chat") or {}
        return "message", user.get("id"), chat.get("id")
    return "unknown", None, None


def _payload_hash(update: dict[str, Any]) -> str:
    raw = json.dumps(update, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def claim_telegram_update(update: dict[str, Any]) -> str:
    raw_update_id = update.get("update_id")
    if raw_update_id is None:
        raise ValueError("Telegram update_id is required")
    update_id = int(raw_update_id)
    update_type, tg_user_id, chat_id = _telegram_update_meta(update)
    payload_hash = _payload_hash(update)
    with db.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO telegram_updates (
                    update_id, update_type, tg_user_id, chat_id, status,
                    processing_started_at, payload_hash
                )
                VALUES (%s, %s, %s, %s, 'processing', now(), %s)
                ON CONFLICT (update_id) DO NOTHING
                RETURNING update_id
                """,
                (update_id, update_type, tg_user_id, chat_id, payload_hash),
            )
            if cur.fetchone():
                return "claimed"
            cur.execute(
                "SELECT status, processing_started_at FROM telegram_updates WHERE update_id = %s FOR UPDATE",
                (update_id,),
            )
            existing = cur.fetchone()
            if not existing:
                raise RuntimeError("Telegram update claim disappeared")
            if existing["status"] == "done":
                return "duplicate_done"
            started_at = existing.get("processing_started_at")
            fresh_processing = bool(
                existing["status"] == "processing"
                and started_at
                and started_at > datetime.now(timezone.utc).astimezone(started_at.tzinfo) - timedelta(minutes=5)
            )
            if fresh_processing:
                return "duplicate_processing"
            cur.execute(
                """
                UPDATE telegram_updates
                SET status = 'processing',
                    attempts = attempts + 1,
                    processing_started_at = now(),
                    finished_at = NULL,
                    last_error = NULL,
                    update_type = %s,
                    tg_user_id = %s,
                    chat_id = %s,
                    payload_hash = %s
                WHERE update_id = %s
                """,
                (update_type, tg_user_id, chat_id, payload_hash, update_id),
            )
            return "reclaimed"


def finish_telegram_update(update_id: int, *, error: str | None = None) -> None:
    db.execute(
        """
        UPDATE telegram_updates
        SET status = %s,
            finished_at = now(),
            last_error = %s
        WHERE update_id = %s
        """,
        ("failed" if error else "done", (error or "")[:500] or None, int(update_id)),
    )


def jobs_status_snapshot() -> dict[str, Any]:
    row = db.fetch_one(
        """
        SELECT
            count(*) FILTER (WHERE status = 'queued') AS queued,
            count(*) FILTER (WHERE status = 'processing') AS processing,
            count(*) FILTER (WHERE status = 'failed') AS failed,
            count(*) FILTER (WHERE status = 'dead') AS dead,
            EXTRACT(EPOCH FROM now() - min(created_at) FILTER (WHERE status = 'queued'))::bigint
                AS oldest_queued_age_seconds,
            count(*) FILTER (
                WHERE status = 'processing' AND locked_at < now() - interval '5 minutes'
            ) AS stale_processing,
            max(finished_at) FILTER (WHERE status = 'done') AS last_done_at
        FROM background_jobs
        """
    ) or {}
    bulk = db.fetch_one(
        """
        SELECT id, status, processed_count, total_count, success_count, failure_count
        FROM bulk_operations
        WHERE status IN ('queued', 'processing')
        ORDER BY created_at DESC
        LIMIT 1
        """
    ) or {}
    sheets = db.fetch_one(
        """
        SELECT
            count(*) FILTER (WHERE sheet_sync_status IN ('queued', 'syncing')) AS queued,
            count(*) FILTER (WHERE sheet_sync_status = 'failed') AS failed
        FROM videos
        """
    ) or {}
    return {
        "queued": int(row.get("queued") or 0),
        "processing": int(row.get("processing") or 0),
        "failed": int(row.get("failed") or 0),
        "dead": int(row.get("dead") or 0),
        "oldest_queued_age_seconds": max(0, int(row["oldest_queued_age_seconds"]))
        if row.get("oldest_queued_age_seconds") is not None
        else None,
        "stale_processing": int(row.get("stale_processing") or 0),
        "last_done_at": row["last_done_at"].isoformat() if row.get("last_done_at") else None,
        "sheets_queued": int(sheets.get("queued") or 0),
        "sheets_failed": int(sheets.get("failed") or 0),
        "bulk": bulk,
    }


def worker_health_snapshot(job_snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    counts = job_snapshot or jobs_status_snapshot()
    try:
        row = db.fetch_one(
            """
            SELECT
                last_started_at,
                last_finished_at,
                last_success_at,
                last_error_at,
                last_error,
                last_claimed,
                last_done,
                last_remaining,
                source,
                invocation_id
            FROM worker_heartbeats
            WHERE worker_name = 'background_jobs'
            """
        ) or {}
    except psycopg.Error:
        row = {}

    last_success_at = row.get("last_success_at")
    seconds_since_last_success: int | None = None
    if last_success_at:
        current = datetime.now(timezone.utc)
        if last_success_at.tzinfo is None:
            last_success_at = last_success_at.replace(tzinfo=timezone.utc)
        seconds_since_last_success = max(0, int((current - last_success_at).total_seconds()))
    queued = int(counts.get("queued") or 0)
    recent = seconds_since_last_success is not None and seconds_since_last_success <= 600
    healthy = bool(recent or queued == 0)
    snapshot = {
        "last_started_at": row["last_started_at"].isoformat() if row.get("last_started_at") else None,
        "last_finished_at": row["last_finished_at"].isoformat() if row.get("last_finished_at") else None,
        "last_success_at": last_success_at.isoformat() if last_success_at else None,
        "last_error_at": row["last_error_at"].isoformat() if row.get("last_error_at") else None,
        "last_error": row.get("last_error"),
        "seconds_since_last_success": seconds_since_last_success,
        "last_claimed": int(row.get("last_claimed") or 0),
        "last_done": int(row.get("last_done") or 0),
        "last_remaining": int(row.get("last_remaining") or 0),
        "source": row.get("source"),
        "invocation_id": row.get("invocation_id"),
        "healthy": healthy,
    }
    if not healthy:
        snapshot["warning"] = "queued jobs are not being processed"
    return snapshot


def format_worker_status(worker: dict[str, Any], job_snapshot: dict[str, Any]) -> str:
    source_labels = {
        "github_actions": "GitHub Actions",
        "vercel_cron": "Vercel Cron",
        "manual": "ручной запуск",
    }
    seconds = worker.get("seconds_since_last_success")
    if seconds is None:
        last_run = "нет успешных запусков"
    elif int(seconds) < 60:
        last_run = f"{int(seconds)} сек. назад"
    else:
        last_run = f"{max(1, int(seconds) // 60)} мин. назад"
    if worker.get("healthy"):
        state = "работает" if seconds is not None and int(seconds) <= 600 else "очередь пуста"
        lines = [
            "Worker",
            "",
            f"Состояние: {state}",
            f"Последний успешный запуск: {last_run}",
            f"Источник: {source_labels.get(worker.get('source'), worker.get('source') or 'неизвестен')}",
        ]
    else:
        age_minutes = max(1, int(seconds) // 60) if seconds is not None else 0
        age_text = f" {age_minutes} мин." if age_minutes else "."
        lines = [
            f"Внимание: Worker не запускался{age_text}",
            f"В очереди: {int(job_snapshot.get('queued') or 0)} заданий.",
            "",
        ]
    lines.extend(
        [
            f"Queued: {int(job_snapshot.get('queued') or 0)}",
            f"Processing: {int(job_snapshot.get('processing') or 0)}",
            f"Failed: {int(job_snapshot.get('failed') or 0)}",
            f"Dead: {int(job_snapshot.get('dead') or 0)}",
        ]
    )
    return "\n".join(lines)


def format_jobs_status(snapshot: dict[str, Any]) -> str:
    age = snapshot.get("oldest_queued_age_seconds")
    lines = [
        "Фоновые задания",
        "",
        f"Queued: {snapshot.get('queued', 0)}",
        f"Processing: {snapshot.get('processing', 0)}",
        f"Failed: {snapshot.get('failed', 0)}",
        f"Dead: {snapshot.get('dead', 0)}",
        f"Старейшее queued: {age if age is not None else 0} сек.",
        "",
        f"Sheets queued: {snapshot.get('sheets_queued', 0)}",
        f"Sheets failed: {snapshot.get('sheets_failed', 0)}",
    ]
    bulk = snapshot.get("bulk") or {}
    if bulk:
        lines.extend(
            [
                "",
                f"Bulk #{bulk['id']}: {bulk.get('processed_count', 0)} из {bulk.get('total_count', 0)}",
            ]
        )
    return "\n".join(lines)


def retry_failed_jobs() -> int:
    with db.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE background_jobs
                SET status = 'queued',
                    available_at = now(),
                    locked_at = NULL,
                    locked_by = NULL,
                    started_at = NULL,
                    last_error = NULL,
                    updated_at = now()
                WHERE status = 'failed'
                  AND attempts < max_attempts
                  AND COALESCE(last_error, '') NOT LIKE 'permanent:%'
                RETURNING id
                """
            )
            rows = list(cur.fetchall())
        for row in rows:
            db.log_event(
                conn,
                entity_type="background_job",
                entity_id=int(row["id"]),
                action="job_retry",
                after_data={"source": "manual_retry"},
            )
    return len(rows)
