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
            "admin_queue": _admin_queue_debug(),
        }
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
