from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler
from typing import Any
from urllib.parse import parse_qs, urlparse

from bot import db, reconciliation, sheet_preambles, sheets
from bot.config import get_settings
from bot.github_oidc import GitHubOIDCError, validate_github_oidc_token
from bot.job_worker import process_jobs

HAMIDULIN_VIDEO_ID = 99
WORLD_CUP_CODE = "world_cup_2026"


def _json(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")


def _status() -> dict[str, Any]:
    run = reconciliation.get_run() or {}
    summary = run.get("summary") if isinstance(run.get("summary"), dict) else {}
    videos = reconciliation.load_active_video_snapshot()
    approved = [video for video in videos if video.get("status") == "approved"]
    regular = sum(str(video.get("video_type") or "regular").lower() != "bigrecap" for video in approved)
    bigrecap = sum(str(video.get("video_type") or "regular").lower() == "bigrecap" for video in approved)
    author_all = [row for row in reconciliation.build_author_work_rows(videos) if row and row[0] == "ALL"]
    montage_all = [row for row in reconciliation.build_montage_work_rows(videos) if row and row[0] == "ALL"]
    ham = db.fetch_one(
        """
        SELECT id,status,video_type,publish_date,youtube_url,youtube_id,
               author_name,author_username,project_code
        FROM videos WHERE id=%s
        """,
        (HAMIDULIN_VIDEO_ID,),
    )
    egor = db.fetch_all(
        """
        SELECT id,name,username,role,is_active
        FROM people
        WHERE lower(name) IN ('егор','егор петрушков')
           OR lower(COALESCE(username,''))='rayballpro'
        ORDER BY role,id
        """
    )
    return {
        "ok": True,
        "run_id": int(run["id"]) if run else None,
        "run_status": run.get("status"),
        "run_stage": run.get("stage"),
        "mismatch_count": reconciliation.run_mismatch_count(run) if run else None,
        "db_active_count": len(videos),
        "approved_count": len(approved),
        "approved_regular": int(regular),
        "approved_bigrecap": int(bigrecap),
        "needs_revision": sum(video.get("status") == "needs_revision" for video in videos),
        "missing_date": sum(reconciliation.publish_month(video) is None for video in videos),
        "project_sheet_counts": summary.get("project_sheet_counts") or {},
        "month_sheet_counts": summary.get("month_sheet_counts") or {},
        "hamidulin": ham,
        "egor_rows": egor,
        "author_report_all": author_all,
        "montage_report_all": montage_all,
        "last_error": run.get("last_error"),
        "finished_at": run.get("finished_at").isoformat() if run and run.get("finished_at") else None,
    }


def _approve_hamidulin() -> dict[str, Any]:
    with db.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id,status,video_type,publish_date,youtube_url,youtube_id,
                       author_name,author_username,project_code
                FROM videos
                WHERE id=%s
                FOR UPDATE
                """,
                (HAMIDULIN_VIDEO_ID,),
            )
            row = cur.fetchone()
            if not row:
                raise RuntimeError("Hamidulin video #99 not found")
            username = str(row.get("author_username") or "").lstrip("@").lower()
            name = str(row.get("author_name") or "").lower()
            if username != "kkkkkk13_13" and "хамидулин" not in name:
                raise RuntimeError("video #99 is not Hamidulin")
            if str(row.get("project_code") or "") != WORLD_CUP_CODE:
                raise RuntimeError("video #99 is not in World Cup 2026")
            if str(row.get("video_type") or "").lower() != "bigrecap":
                raise RuntimeError("video #99 is not bigrecap")
            if not row.get("publish_date"):
                raise RuntimeError("video #99 has no publish_date")
            if not row.get("youtube_url") and not row.get("youtube_id"):
                raise RuntimeError("video #99 has no YouTube link")
            changed = False
            if row.get("status") != "approved":
                cur.execute(
                    """
                    UPDATE videos
                    SET status='approved',
                        checked_at=COALESCE(checked_at,now()),
                        sheet_sync_status='queued',
                        sheet_sync_error=NULL,
                        updated_at=now()
                    WHERE id=%s
                    """,
                    (HAMIDULIN_VIDEO_ID,),
                )
                changed = True
                db.log_event(
                    conn,
                    entity_type="video",
                    entity_id=HAMIDULIN_VIDEO_ID,
                    action="user_confirmed_bigrecap_counted",
                    actor_username="system:finalize-report-v1.0.20",
                    before_data={"status": row.get("status")},
                    after_data={"status": "approved", "video_type": "bigrecap"},
                )
    return {"changed": changed, "video": db.fetch_one("SELECT id,status,video_type,publish_date,author_name,author_username FROM videos WHERE id=%s", (HAMIDULIN_VIDEO_ID,))}


def _ensure_audit() -> dict[str, Any]:
    ham = _approve_hamidulin()
    run = reconciliation.get_run()
    if run and run.get("status") == "auditing":
        # The previous one-time runner lost auth while a drain request was in flight.
        # Re-running the read-only audit is safe and deterministically moves the run
        # to awaiting_confirmation.
        run = reconciliation.audit_run(int(run["id"]))
    elif not run or run.get("status") in {"done", "failed", "cancelled"}:
        settings = get_settings()
        run_id = reconciliation.create_audit_run(
            actor_tg_id=0,
            actor_username="system:finalize-report-v1.0.20",
            chat_id=int(settings.admin_chat_id),
        )
        run = reconciliation.get_run(run_id)
    return {"ok": True, "hamidulin": ham, "status": _status()}


def _confirm() -> dict[str, Any]:
    run = reconciliation.get_run()
    if not run:
        raise RuntimeError("reconciliation run not found")
    changed = False
    if run.get("status") == "awaiting_confirmation":
        changed = reconciliation.confirm_run(
            int(run["id"]),
            mode="db_only",
            actor_tg_id=0,
            actor_username="system:finalize-report-v1.0.20",
        )
    return {"ok": True, "confirmed": changed, "status": _status()}


def _postprocess() -> dict[str, Any]:
    run = reconciliation.get_run()
    if not run or run.get("status") != "done":
        raise RuntimeError("reconciliation is not done")
    service = sheets._service()
    videos = reconciliation.load_active_video_snapshot()
    sheets.normalize_metrics_sheet_layout(service=service)
    deleted_tabs = sheets.cleanup_empty_report_tabs(videos, service=service)
    preambles = sheet_preambles.normalize_all_existing_sheet_preambles(service=service)
    properties = sheets._sheet_properties(service, get_settings().google_sheets_spreadsheet_id)
    return {
        "ok": not preambles.get("failures"),
        "deleted_tabs": deleted_tabs,
        "preambles": preambles,
        "people_projects_exists": sheets.PEOPLE_PROJECTS_SHEET_NAME in properties,
        "status": _status(),
    }


class handler(BaseHTTPRequestHandler):
    def _send(self, code: int, payload: dict[str, Any]) -> None:
        body = _json(payload)
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        auth = self.headers.get("Authorization") or ""
        scheme, _, token = auth.partition(" ")
        if scheme.lower() != "bearer" or token.count(".") != 2:
            return False
        try:
            validate_github_oidc_token(token)
            return True
        except GitHubOIDCError:
            return False

    def do_GET(self) -> None:
        if not self._authorized():
            self._send(401, {"ok": False, "error": "unauthorized"})
            return
        action = parse_qs(urlparse(self.path).query).get("action", ["status"])[0]
        try:
            if action == "status":
                payload = _status()
            elif action == "ensure":
                payload = _ensure_audit()
            elif action == "confirm":
                payload = _confirm()
            elif action == "drain":
                payload = {"ok": True, "worker": process_jobs(source="github_actions"), "status": _status()}
            elif action == "postprocess":
                payload = _postprocess()
            else:
                self._send(400, {"ok": False, "error": "unsupported action"})
                return
        except Exception as exc:
            self._send(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:800]})
            return
        self._send(200, payload)

    def do_POST(self) -> None:
        self.do_GET()
