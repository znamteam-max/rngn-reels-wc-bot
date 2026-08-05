from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler
from typing import Any
from urllib.parse import parse_qs, urlparse

from bot import db, reconciliation
from bot.config import get_settings
from bot.github_oidc import GitHubOIDCError, validate_github_oidc_token
from bot.job_worker import process_jobs


TARGET_CODE = "ves_sport"
TARGET_NAME = "Весь Спорт"


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _latest_run() -> dict[str, Any] | None:
    return reconciliation.get_run()


def _status_payload() -> dict[str, Any]:
    run = _latest_run() or {}
    project_rows = db.fetch_all(
        """
        SELECT COALESCE(NULLIF(project_code, ''), 'unassigned') AS project_code,
               count(*) AS count
        FROM videos
        WHERE status <> 'deleted'
        GROUP BY 1
        ORDER BY 1
        """
    )
    status_rows = db.fetch_all(
        """
        SELECT status, count(*) AS count
        FROM videos
        WHERE status <> 'deleted'
        GROUP BY status
        ORDER BY status
        """
    )
    job_rows = db.fetch_all(
        """
        SELECT status, count(*) AS count
        FROM background_jobs
        WHERE status IN ('queued','processing','failed','dead')
        GROUP BY status
        ORDER BY status
        """
    )
    summary = run.get("summary") if isinstance(run.get("summary"), dict) else {}
    settings = get_settings()
    return {
        "ok": True,
        "run_id": int(run["id"]) if run else None,
        "run_status": run.get("status"),
        "run_stage": run.get("stage"),
        "run_mode": run.get("mode"),
        "db_active_count": int(run.get("db_active_count") or 0),
        "db_approved_count": int(run.get("db_approved_count") or 0),
        "db_pending_count": int(run.get("db_pending_count") or 0),
        "db_needs_revision_count": int(run.get("db_needs_revision_count") or 0),
        "db_duplicate_count": int(run.get("db_duplicate_count") or 0),
        "db_unassigned_count": int(run.get("db_unassigned_count") or 0),
        "db_missing_date_count": int(run.get("db_missing_date_count") or 0),
        "sheet_videos_count": int(run.get("sheet_videos_count") or 0),
        "sheet_project_union_count": int(run.get("sheet_project_union_count") or 0),
        "sheet_month_union_count": int(run.get("sheet_month_union_count") or 0),
        "mismatch_count": reconciliation.run_mismatch_count(run) if run else None,
        "changed_project_ids": summary.get("changed_project_ids") or [],
        "db_project_counts": summary.get("db_project_counts") or {
            str(row["project_code"]): int(row["count"]) for row in project_rows
        },
        "project_counts_live": {
            str(row["project_code"]): int(row["count"]) for row in project_rows
        },
        "status_counts_live": {str(row["status"]): int(row["count"]) for row in status_rows},
        "month_sheet_counts": summary.get("month_sheet_counts") or {},
        "project_sheet_counts": summary.get("project_sheet_counts") or {},
        "unfinished_request_count": int(summary.get("unfinished_request_count") or 0),
        "last_error": run.get("last_error"),
        "jobs": {str(row["status"]): int(row["count"]) for row in job_rows},
        "work_chat_id_present": bool(getattr(settings, "work_chat_id", None)),
        "updated_at": run.get("updated_at").isoformat() if run and run.get("updated_at") else None,
        "finished_at": run.get("finished_at").isoformat() if run and run.get("finished_at") else None,
    }


def _apply_assignment() -> dict[str, Any]:
    changed_ids: list[int] = []
    before_rows: list[dict[str, Any]] = []
    with db.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM projects WHERE code=%s LIMIT 1", (TARGET_CODE,))
            project = cur.fetchone()
            if not project:
                raise RuntimeError("target project not found")
            project_id = int(project["id"])
            cur.execute(
                """
                SELECT id, project_code, project_name, project_id
                FROM videos
                WHERE status <> 'deleted'
                  AND (
                    project_code IS DISTINCT FROM %s
                    OR project_name IS DISTINCT FROM %s
                    OR project_id IS DISTINCT FROM %s
                  )
                ORDER BY id
                FOR UPDATE
                """,
                (TARGET_CODE, TARGET_NAME, project_id),
            )
            before_rows = list(cur.fetchall())
            changed_ids = [int(row["id"]) for row in before_rows]
            if changed_ids:
                cur.execute(
                    """
                    UPDATE videos
                    SET project_code=%s,
                        project_name=%s,
                        project_id=%s,
                        sheet_sync_status='queued',
                        sheet_sync_error=NULL,
                        updated_at=now()
                    WHERE id = ANY(%s)
                    """,
                    (TARGET_CODE, TARGET_NAME, project_id, changed_ids),
                )
            for row in before_rows:
                db.log_event(
                    conn,
                    entity_type="video",
                    entity_id=int(row["id"]),
                    action="world_cup_project_corrected",
                    actor_username="system:world-cup-reconciliation",
                    before_data={
                        "project_code": row.get("project_code"),
                        "project_name": row.get("project_name"),
                        "project_id": row.get("project_id"),
                    },
                    after_data={
                        "project_code": TARGET_CODE,
                        "project_name": TARGET_NAME,
                        "project_id": project_id,
                    },
                )
        db.log_event(
            conn,
            entity_type="sheet_reconciliation",
            entity_id=None,
            action="all_world_cup_assigned_to_ves_sport",
            actor_username="system:world-cup-reconciliation",
            after_data={"changed_count": len(changed_ids), "changed_ids": changed_ids},
        )

    settings = get_settings()
    run_id = reconciliation.create_audit_run(
        actor_tg_id=0,
        actor_username="system:world-cup-reconciliation",
        chat_id=int(settings.admin_chat_id),
    )
    return {
        "ok": True,
        "changed_count": len(changed_ids),
        "changed_ids": changed_ids,
        "run_id": run_id,
        "status": _status_payload(),
    }


def _confirm_rebuild() -> dict[str, Any]:
    run = _latest_run()
    if not run:
        raise RuntimeError("reconciliation run not found")
    run_id = int(run["id"])
    confirmed = False
    if run.get("status") == "awaiting_confirmation":
        confirmed = reconciliation.confirm_run(
            run_id,
            mode="db_only",
            actor_tg_id=0,
            actor_username="system:world-cup-reconciliation",
        )
    return {"ok": True, "confirmed_now": confirmed, "status": _status_payload()}


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
                payload = _status_payload()
            elif action == "apply":
                payload = _apply_assignment()
            elif action == "confirm":
                payload = _confirm_rebuild()
            elif action == "drain":
                payload = {"ok": True, "worker": process_jobs(source="github_actions"), "status": _status_payload()}
            else:
                self._send_json(400, {"ok": False, "error": "unsupported action"})
                return
        except Exception as exc:
            self._send_json(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:500]})
            return
        self._send_json(200, payload)

    def do_POST(self) -> None:
        self.do_GET()

    def do_HEAD(self) -> None:
        self._send_json(405, {"ok": False, "error": "method not allowed"})
