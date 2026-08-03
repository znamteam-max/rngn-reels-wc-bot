from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler

from bot import db
from bot.config import missing_env_names, optional_missing_env_names
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


class handler(BaseHTTPRequestHandler):
    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        runtime_migration = ensure_runtime_migrations()
        payload = {
            "ok": True,
            "service": "rngn-reels-wc-bot",
            "version": VERSION,
            "commit_sha": os.environ.get("VERCEL_GIT_COMMIT_SHA"),
            "time": datetime.now(timezone.utc).isoformat(),
            "missing_env": missing_env_names(),
            "optional_missing_env": optional_missing_env_names(),
            "runtime_migration": runtime_migration,
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
