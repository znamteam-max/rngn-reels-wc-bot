from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler

from bot import db, jobs, worker_kick
from bot.config import get_settings, missing_env_names, optional_missing_env_names
from bot.runtime_migrations import ensure_runtime_migrations
from bot.version import VERSION


def _admin_queue_debug() -> dict[str, object]:
    row = db.fetch_one(
        """
        SELECT
            (SELECT count(*) FROM videos WHERE status = 'pending') AS pending_video_count,
            q.active_video_id AS active_queue_video_id,
            q.active_message_id AS active_queue_message_id,
            q.dashboard_message_id,
            q.dashboard_updated_at,
            q.queue_filter_type,
            q.queue_filter_value,
            EXTRACT(
                EPOCH FROM now() - (
                    SELECT min(created_at) FROM videos WHERE status = 'pending'
                )
            )::bigint AS oldest_pending_age_seconds,
            v.status AS active_queue_video_status
        FROM admin_queue_state q
        LEFT JOIN videos v ON v.id = q.active_video_id
        WHERE q.queue_name = 'main'
        """
    ) or {}
    return {
        "pending_video_count": int(row.get("pending_video_count") or 0),
        "active_queue_video_id": row.get("active_queue_video_id"),
        "active_queue_message_id": row.get("active_queue_message_id"),
        "active_queue_video_status": row.get("active_queue_video_status"),
        "dashboard_message_id": row.get("dashboard_message_id"),
        "dashboard_updated_at": row["dashboard_updated_at"].isoformat() if row.get("dashboard_updated_at") else None,
        "queue_filter_type": row.get("queue_filter_type") or "global",
        "queue_filter_value": row.get("queue_filter_value"),
        "oldest_pending_age_seconds": max(0, int(row["oldest_pending_age_seconds"]))
        if row.get("oldest_pending_age_seconds") is not None
        else None,
    }


def _projects_debug() -> dict[str, int]:
    row = db.fetch_one(
        """
        SELECT
            (SELECT count(*) FROM projects WHERE is_active = true) AS active_count,
            (
                SELECT count(*)
                FROM videos
                WHERE COALESCE(project_code, '') = '' OR COALESCE(project_name, '') = ''
            ) AS videos_without_project
        """
    ) or {}
    return {
        "active_count": int(row.get("active_count") or 0),
        "videos_without_project": int(row.get("videos_without_project") or 0),
    }


def _daily_report_debug() -> dict[str, object]:
    row = db.fetch_one(
        """
        SELECT report_date, telegram_message_id
        FROM daily_reports
        ORDER BY report_date DESC
        LIMIT 1
        """
    ) or {}
    return {
        "last_report_date": row["report_date"].isoformat() if row.get("report_date") else None,
        "last_report_message_id": row.get("telegram_message_id"),
    }


def _missing_publish_date_debug() -> dict[str, int]:
    row = db.fetch_one(
        """
        SELECT
            count(*) FILTER (WHERE status = 'pending') AS pending,
            count(*) FILTER (WHERE status = 'needs_revision') AS needs_revision
        FROM videos
        WHERE publish_date IS NULL
          AND status IN ('pending', 'needs_revision')
        """
    ) or {}
    return {
        "pending": int(row.get("pending") or 0),
        "needs_revision": int(row.get("needs_revision") or 0),
    }


def _egor_montage_debug() -> dict[str, int]:
    row = db.fetch_one(
        """
        SELECT
            (
                SELECT count(*)
                FROM people
                WHERE role = 'montage'
                  AND name = 'Егор Петрушков'
                  AND username = 'RayBallPro'
                  AND is_active = true
            ) AS active_rows,
            (
                SELECT count(*)
                FROM videos
                WHERE montage_name = 'Егор Петрушков'
                  AND montage_username = 'RayBallPro'
            ) AS backfilled_videos
        """
    ) or {}
    return {
        "active_rows": int(row.get("active_rows") or 0),
        "backfilled_videos": int(row.get("backfilled_videos") or 0),
    }


def _jobs_debug() -> dict[str, object]:
    row = db.fetch_one(
        """
        SELECT
            count(*) FILTER (WHERE status = 'queued') AS queued,
            count(*) FILTER (WHERE status = 'queued' AND available_at <= now()) AS ready,
            count(*) FILTER (WHERE status = 'queued' AND available_at > now()) AS future,
            count(*) FILTER (WHERE status = 'processing') AS processing,
            count(*) FILTER (WHERE status = 'failed') AS failed,
            count(*) FILTER (WHERE status = 'dead') AS dead,
            EXTRACT(EPOCH FROM now() - min(created_at) FILTER (WHERE status = 'queued'))::bigint
                AS oldest_queued_age_seconds,
            EXTRACT(
                EPOCH FROM now() - min(available_at)
                FILTER (WHERE status = 'queued' AND available_at <= now())
            )::bigint AS oldest_ready_age_seconds,
            count(*) FILTER (
                WHERE status = 'processing' AND locked_at < now() - interval '5 minutes'
            ) AS stale_processing,
            max(finished_at) FILTER (WHERE status = 'done') AS last_done_at
        FROM background_jobs
        """
    ) or {}
    return {
        "enabled": get_settings().background_jobs_enabled,
        "queued": int(row.get("queued") or 0),
        "ready": int(row.get("ready") or 0),
        "future": int(row.get("future") or 0),
        "processing": int(row.get("processing") or 0),
        "failed": int(row.get("failed") or 0),
        "dead": int(row.get("dead") or 0),
        "oldest_queued_age_seconds": max(0, int(row["oldest_queued_age_seconds"]))
        if row.get("oldest_queued_age_seconds") is not None
        else None,
        "oldest_ready_age_seconds": max(0, int(row["oldest_ready_age_seconds"]))
        if row.get("oldest_ready_age_seconds") is not None
        else None,
        "stale_processing": int(row.get("stale_processing") or 0),
        "last_done_at": row["last_done_at"].isoformat() if row.get("last_done_at") else None,
    }


def _sheets_debug() -> dict[str, int]:
    row = db.fetch_one(
        """
        SELECT
            count(*) FILTER (WHERE sheet_sync_status IN ('queued', 'syncing')) AS queued_videos,
            count(*) FILTER (WHERE sheet_sync_status = 'failed') AS failed_videos
        FROM videos
        """
    ) or {}
    return {
        "queued_videos": int(row.get("queued_videos") or 0),
        "failed_videos": int(row.get("failed_videos") or 0),
    }


def _telegram_updates_debug() -> dict[str, int]:
    row = db.fetch_one(
        """
        SELECT
            count(*) FILTER (
                WHERE status = 'failed' AND finished_at >= now() - interval '1 hour'
            ) AS failed_last_hour,
            count(*) FILTER (
                WHERE status = 'processing'
                  AND processing_started_at < now() - interval '5 minutes'
            ) AS processing_stale
        FROM telegram_updates
        """
    ) or {}
    return {
        "failed_last_hour": int(row.get("failed_last_hour") or 0),
        "processing_stale": int(row.get("processing_stale") or 0),
    }


def _bulk_operations_debug() -> dict[str, int]:
    row = db.fetch_one(
        "SELECT count(*) AS active FROM bulk_operations WHERE status IN ('queued', 'processing')"
    ) or {}
    return {"active": int(row.get("active") or 0)}


def _webhook_performance_debug() -> dict[str, object]:
    row = db.fetch_one(
        """
        WITH timings AS (
            SELECT (after_data->>'duration_ms')::numeric AS duration_ms
            FROM logs
            WHERE action = 'webhook_done'
              AND created_at >= now() - interval '1 hour'
              AND COALESCE(after_data->>'duration_ms', '') ~ '^[0-9]+$'
        )
        SELECT
            count(*) AS samples,
            percentile_cont(0.50) WITHIN GROUP (ORDER BY duration_ms) AS p50_ms,
            percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms) AS p95_ms
        FROM timings
        """
    ) or {}
    return {
        "window_minutes": 60,
        "samples": int(row.get("samples") or 0),
        "p50_ms": round(float(row["p50_ms"]), 1) if row.get("p50_ms") is not None else None,
        "p95_ms": round(float(row["p95_ms"]), 1) if row.get("p95_ms") is not None else None,
        "target_p95_ms": 3000,
    }


class handler(BaseHTTPRequestHandler):
    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        rollout_migration = ensure_runtime_migrations(force=True)
        rollout_kick = worker_kick.kick_worker_if_ready(
            reason="v1.0.17_rollout",
            force=True,
        )
        job_debug = _jobs_debug()
        kick_debug = worker_kick.worker_kick_snapshot()
        payload = {
            "ok": True,
            "service": "rngn-reels-wc-bot",
            "version": VERSION,
            "commit_sha": os.environ.get("VERCEL_GIT_COMMIT_SHA"),
            "time": datetime.now(timezone.utc).isoformat(),
            "missing_env": missing_env_names(),
            "optional_missing_env": optional_missing_env_names(),
            "schema_version": db.current_schema_version(),
            "rollout_migration": rollout_migration,
            "rollout_kick": rollout_kick,
            "database": db.pool_diagnostics(),
            "jobs": job_debug,
            "worker": jobs.worker_health_snapshot(job_debug, kick_debug),
            "worker_kick": kick_debug,
            "sheets": _sheets_debug(),
            "telegram_updates": _telegram_updates_debug(),
            "bulk_operations": _bulk_operations_debug(),
            "webhook_performance": _webhook_performance_debug(),
            "projects": _projects_debug(),
            "admin_queue": _admin_queue_debug(),
            "daily_report": _daily_report_debug(),
            "missing_publish_date": _missing_publish_date_debug(),
            "egor_montage": _egor_montage_debug(),
        }
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
