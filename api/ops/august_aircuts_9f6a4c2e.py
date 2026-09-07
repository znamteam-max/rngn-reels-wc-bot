from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler
from typing import Any
from urllib.parse import parse_qs, urlparse

from bot import db, jobs
from bot.project_workflow_patch import AIR_CUT_MARKER, VM_PROJECT_CODE


ONE_TIME_KEY = "9f6a4c2e7b814d5fa3c1e908d26b7a51"
TARGET_COUNT = 8


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")


def _mark_comment(value: Any) -> str:
    text = str(value or "").strip()
    if text.startswith(AIR_CUT_MARKER):
        return text
    if text:
        return f"{AIR_CUT_MARKER}\n\n{text}"
    return AIR_CUT_MARKER


def _load_candidates(conn) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, status, project_code, project_name, publish_date, comment, created_at
            FROM videos
            WHERE status = 'pending'
              AND project_code = %s
              AND publish_date >= DATE '2026-08-01'
              AND publish_date < DATE '2026-09-01'
            ORDER BY created_at ASC, id ASC
            LIMIT %s
            FOR UPDATE
            """,
            (VM_PROJECT_CODE, TARGET_COUNT),
        )
        return list(cur.fetchall())


class handler(BaseHTTPRequestHandler):
    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = _json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self) -> None:
        query = parse_qs(urlparse(self.path).query)
        if (query.get("key") or [""])[0] != ONE_TIME_KEY:
            self._send_json(404, {"ok": False})
            return
        mode = (query.get("mode") or ["preview"])[0]
        if mode not in {"preview", "apply"}:
            self._send_json(400, {"ok": False, "error": "mode must be preview or apply"})
            return

        try:
            with db.transaction() as conn:
                candidates = _load_candidates(conn)
                snapshot = [
                    {
                        "id": int(row["id"]),
                        "publish_date": row.get("publish_date"),
                        "already_aircut": str(row.get("comment") or "").strip().startswith(AIR_CUT_MARKER),
                    }
                    for row in candidates
                ]
                if mode == "apply":
                    for row in candidates:
                        new_comment = _mark_comment(row.get("comment"))
                        with conn.cursor() as cur:
                            cur.execute(
                                """
                                UPDATE videos
                                SET comment = %s,
                                    updated_at = now()
                                WHERE id = %s
                                  AND status = 'pending'
                                  AND project_code = %s
                                """,
                                (new_comment, int(row["id"]), VM_PROJECT_CODE),
                            )
                        db.log_event(
                            conn,
                            entity_type="video",
                            entity_id=int(row["id"]),
                            action="aircut_backfill_august_2026",
                            before_data={"comment": row.get("comment")},
                            after_data={"comment": new_comment},
                        )
        except Exception as exc:
            self._send_json(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]})
            return

        sheet_jobs: dict[str, int | None] = {}
        if mode == "apply":
            for row in candidates:
                video_id = int(row["id"])
                try:
                    sheet_jobs[str(video_id)] = jobs.enqueue_sheet_sync(video_id, version="aircut-backfill-2026-08")
                except Exception:
                    sheet_jobs[str(video_id)] = None

        self._send_json(
            200,
            {
                "ok": True,
                "mode": mode,
                "target_count": TARGET_COUNT,
                "candidate_count": len(candidates),
                "candidates": snapshot,
                "sheet_jobs": sheet_jobs,
            },
        )

    def do_HEAD(self) -> None:
        self.do_GET()
