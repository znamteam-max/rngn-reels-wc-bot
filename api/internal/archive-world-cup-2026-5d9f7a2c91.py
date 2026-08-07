from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler
from typing import Any
from urllib.parse import parse_qs, urlparse

from bot import db, reconciliation
from bot.github_oidc import GitHubOIDCError, validate_github_oidc_token
from bot.job_worker import process_jobs

ARCHIVE_CODE = "world_cup_2026"
ARCHIVE_NAME = "ЧМ 2026"
SOURCE_CODE = "ves_sport"


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _latest_run() -> dict[str, Any] | None:
    return reconciliation.get_run()


def _status() -> dict[str, Any]:
    run = _latest_run() or {}
    summary = run.get("summary") if isinstance(run.get("summary"), dict) else {}
    project_rows = db.fetch_all(
        """
        SELECT COALESCE(NULLIF(project_code, ''), 'unassigned') AS project_code, count(*) AS count
        FROM videos
        WHERE status <> 'deleted'
        GROUP BY 1
        ORDER BY 1
        """
    )
    return {
        "ok": True,
        "run_id": int(run["id"]) if run else None,
        "run_status": run.get("status"),
        "run_stage": run.get("stage"),
        "run_mode": run.get("mode"),
        "db_active_count": int(run.get("db_active_count") or 0),
        "sheet_videos_count": int(run.get("sheet_videos_count") or 0),
        "sheet_project_union_count": int(run.get("sheet_project_union_count") or 0),
        "sheet_month_union_count": int(run.get("sheet_month_union_count") or 0),
        "db_approved_count": int(run.get("db_approved_count") or 0),
        "db_needs_revision_count": int(run.get("db_needs_revision_count") or 0),
        "db_missing_date_count": int(run.get("db_missing_date_count") or 0),
        "mismatch_count": reconciliation.run_mismatch_count(run) if run else None,
        "project_counts_live": {str(r["project_code"]): int(r["count"]) for r in project_rows},
        "project_sheet_counts": summary.get("project_sheet_counts") or {},
        "month_sheet_counts": summary.get("month_sheet_counts") or {},
        "unfinished_request_count": int(summary.get("unfinished_request_count") or 0),
        "last_error": run.get("last_error"),
        "finished_at": run.get("finished_at").isoformat() if run and run.get("finished_at") else None,
    }


def _apply() -> dict[str, Any]:
    changed_ids: list[int] = []
    with db.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO projects (code, name, emoji, is_active, sort_order, updated_at)
                VALUES (%s, %s, '🏆', false, 31, now())
                ON CONFLICT (code) DO UPDATE SET
                    name=EXCLUDED.name,
                    emoji=EXCLUDED.emoji,
                    is_active=false,
                    sort_order=EXCLUDED.sort_order,
                    updated_at=now()
                RETURNING id
                """,
                (ARCHIVE_CODE, ARCHIVE_NAME),
            )
            project_id = int(cur.fetchone()["id"])
            cur.execute(
                """
                SELECT id
                FROM videos
                WHERE status <> 'deleted' AND project_code = %s
                ORDER BY id
                FOR UPDATE
                """,
                (SOURCE_CODE,),
            )
            changed_ids = [int(row["id"]) for row in cur.fetchall()]
            if changed_ids:
                cur.execute(
                    """
                    UPDATE videos
                    SET project_id=%s,
                        project_code=%s,
                        project_name=%s,
                        sheet_sync_status='queued',
                        sheet_sync_error=NULL,
                        updated_at=now()
                    WHERE id = ANY(%s)
                    """,
                    (project_id, ARCHIVE_CODE, ARCHIVE_NAME, changed_ids),
                )
            db.log_event(
                conn,
                entity_type="sheet_reconciliation",
                entity_id=None,
                action="world_cup_2026_archived",
                actor_username="system:world-cup-2026-archive",
                after_data={"changed_count": len(changed_ids), "changed_ids": changed_ids},
            )

    # Start a fresh audit after mutation.
    settings = __import__("bot.config", fromlist=["get_settings"]).get_settings()
    run_id = reconciliation.create_audit_run(
        actor_tg_id=0,
        actor_username="system:world-cup-2026-archive",
        chat_id=int(settings.admin_chat_id),
    )
    return {"ok": True, "changed_count": len(changed_ids), "changed_ids": changed_ids, "run_id": run_id, "status": _status()}


def _confirm() -> dict[str, Any]:
    run = _latest_run()
    if not run:
        raise RuntimeError("reconciliation run not found")
    confirmed = False
    if run.get("status") == "awaiting_confirmation":
        confirmed = reconciliation.confirm_run(
            int(run["id"]),
            mode="db_only",
            actor_tg_id=0,
            actor_username="system:world-cup-2026-archive",
        )
    return {"ok": True, "confirmed_now": confirmed, "status": _status()}


class handler(BaseHTTPRequestHandler):
    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = _json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authenticated(self) -> bool:
        authorization = self.headers.get("Authorization") or ""
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or token.count(".") != 2:
            return False
        try:
            validate_github_oidc_token(token)
        except GitHubOIDCError:
            return False
        return True

    def do_GET(self) -> None:
        if not self._authenticated():
            self._send_json(401, {"ok": False, "error": "unauthorized"})
            return
        action = parse_qs(urlparse(self.path).query).get("action", ["status"])[0]
        try:
            if action == "status":
                payload = _status()
            elif action == "apply":
                payload = _apply()
            elif action == "confirm":
                payload = _confirm()
            elif action == "drain":
                payload = {"ok": True, "worker": process_jobs(source="github_actions"), "status": _status()}
            else:
                self._send_json(400, {"ok": False, "error": "unsupported action"})
                return
        except Exception as exc:
            self._send_json(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:500]})
            return
        self._send_json(200, payload)

    def do_POST(self) -> None:
        self.do_GET()
