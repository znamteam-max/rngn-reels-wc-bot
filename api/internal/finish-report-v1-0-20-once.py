from __future__ import annotations

import json
import time
from http.server import BaseHTTPRequestHandler
from typing import Any

from bot import db, reconciliation, sheet_preambles, sheets
from bot.config import get_settings
from bot.github_oidc import GitHubOIDCError, validate_github_oidc_token
from bot.job_worker import process_jobs

HAM_ID = 99


def _body(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")


def _approve_hamidulin() -> bool:
    with db.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id,status,video_type,publish_date,youtube_url,youtube_id,
                       author_name,author_username,project_code
                FROM videos WHERE id=%s FOR UPDATE
                """,
                (HAM_ID,),
            )
            row = cur.fetchone()
            if not row:
                raise RuntimeError("Hamidulin video #99 not found")
            username = str(row.get("author_username") or "").lstrip("@").lower()
            name = str(row.get("author_name") or "").lower()
            if username != "kkkkkk13_13" and "хамидулин" not in name:
                raise RuntimeError("video #99 is not Hamidulin")
            if row.get("project_code") != "world_cup_2026":
                raise RuntimeError("video #99 is outside World Cup 2026")
            if str(row.get("video_type") or "").lower() != "bigrecap":
                raise RuntimeError("video #99 is not marked bigrecap")
            if not row.get("publish_date") or (not row.get("youtube_url") and not row.get("youtube_id")):
                raise RuntimeError("video #99 is missing date or YouTube link")
            if row.get("status") == "approved":
                return False
            cur.execute(
                """
                UPDATE videos
                SET status='approved', checked_at=COALESCE(checked_at,now()),
                    sheet_sync_status='queued', sheet_sync_error=NULL, updated_at=now()
                WHERE id=%s
                """,
                (HAM_ID,),
            )
            db.log_event(
                conn,
                entity_type="video",
                entity_id=HAM_ID,
                action="user_confirmed_bigrecap_counted",
                actor_username="system:report-v1.0.20-single-call",
                before_data={"status": row.get("status")},
                after_data={"status": "approved", "video_type": "bigrecap"},
            )
            return True


def _snapshot() -> dict[str, Any]:
    run = reconciliation.get_run() or {}
    summary = run.get("summary") if isinstance(run.get("summary"), dict) else {}
    videos = reconciliation.load_active_video_snapshot()
    approved = [v for v in videos if v.get("status") == "approved"]
    regular = sum(str(v.get("video_type") or "regular").lower() != "bigrecap" for v in approved)
    big = sum(str(v.get("video_type") or "regular").lower() == "bigrecap" for v in approved)
    authors = [row for row in reconciliation.build_author_work_rows(videos) if row and row[0] == "ALL"]
    montage = [row for row in reconciliation.build_montage_work_rows(videos) if row and row[0] == "ALL"]
    return {
        "run_id": int(run["id"]) if run else None,
        "run_status": run.get("status"),
        "run_stage": run.get("stage"),
        "mismatch_count": reconciliation.run_mismatch_count(run) if run else None,
        "active": len(videos),
        "approved": len(approved),
        "regular": int(regular),
        "bigrecap": int(big),
        "needs_revision": sum(v.get("status") == "needs_revision" for v in videos),
        "missing_date": sum(reconciliation.publish_month(v) is None for v in videos),
        "project_sheet_counts": summary.get("project_sheet_counts") or {},
        "month_sheet_counts": summary.get("month_sheet_counts") or {},
        "authors_all": authors,
        "montage_all": montage,
        "hamidulin": db.fetch_one("SELECT id,status,video_type,publish_date,author_name,author_username FROM videos WHERE id=%s", (HAM_ID,)),
        "egor": db.fetch_all(
            """
            SELECT id,name,username,role,is_active FROM people
            WHERE lower(name) IN ('егор','егор петрушков')
               OR lower(COALESCE(username,''))='rayballpro'
            ORDER BY role,id
            """
        ),
    }


def _finish() -> dict[str, Any]:
    started = time.monotonic()
    ham_changed = _approve_hamidulin()
    run = reconciliation.get_run()

    if not run or run.get("status") in {"failed", "cancelled"} or (run.get("status") == "done" and ham_changed):
        settings = get_settings()
        run_id = reconciliation.create_audit_run(
            actor_tg_id=0,
            actor_username="system:report-v1.0.20-single-call",
            chat_id=int(settings.admin_chat_id),
        )
        run = reconciliation.get_run(run_id)

    if run and run.get("status") in {"created", "auditing"}:
        run = reconciliation.audit_run(int(run["id"]))

    if run and run.get("status") == "awaiting_confirmation":
        reconciliation.confirm_run(
            int(run["id"]),
            mode="db_only",
            actor_tg_id=0,
            actor_username="system:report-v1.0.20-single-call",
        )

    worker_calls = 0
    while time.monotonic() - started < 245:
        run = reconciliation.get_run()
        if run and run.get("status") == "done":
            break
        if run and run.get("status") in {"failed", "cancelled"}:
            raise RuntimeError(f"reconciliation ended as {run.get('status')}: {run.get('last_error')}")
        if run and run.get("status") == "awaiting_confirmation":
            reconciliation.confirm_run(
                int(run["id"]),
                mode="db_only",
                actor_tg_id=0,
                actor_username="system:report-v1.0.20-single-call",
            )
        process_jobs(source="github_actions")
        worker_calls += 1
        time.sleep(0.35)
    else:
        raise RuntimeError("reconciliation did not finish inside single-call budget")

    service = sheets._service()
    videos = reconciliation.load_active_video_snapshot()
    sheets.normalize_metrics_sheet_layout(service=service)
    deleted_tabs = sheets.cleanup_empty_report_tabs(videos, service=service)
    preambles = sheet_preambles.normalize_all_existing_sheet_preambles(service=service)
    properties = sheets._sheet_properties(service, get_settings().google_sheets_spreadsheet_id)
    final = _snapshot()

    authors = final["authors_all"]
    egor_rows = [row for row in authors if len(row) >= 8 and row[1] == "Егор Петрушков"]
    ham_rows = [row for row in authors if len(row) >= 8 and "Хамидулин" in row[1]]
    checks = {
        "run_done": final["run_status"] == "done",
        "mismatch_zero": int(final["mismatch_count"] or 0) == 0,
        "active_309": final["active"] == 309,
        "approved_309": final["approved"] == 309,
        "regular_295": final["regular"] == 295,
        "bigrecap_14": final["bigrecap"] == 14,
        "needs_revision_zero": final["needs_revision"] == 0,
        "missing_date_zero": final["missing_date"] == 0,
        "world_cup_309": int((final["project_sheet_counts"] or {}).get("ЧМ 2026") or 0) == 309,
        "ves_sport_zero": int((final["project_sheet_counts"] or {}).get("Весь Спорт") or 0) == 0,
        "egor_one_author_row": len(egor_rows) == 1 and egor_rows[0][2] == "@RayBallPro",
        "hamidulin_bigrecap_counted": len(ham_rows) == 1 and int(ham_rows[0][6]) == 1,
        "all_pages_have_explanation": not preambles.get("failures"),
        "legacy_people_projects_removed": sheets.PEOPLE_PROJECTS_SHEET_NAME not in properties,
    }
    return {
        "ok": all(checks.values()),
        "hamidulin_changed": ham_changed,
        "worker_calls": worker_calls,
        "deleted_tabs": deleted_tabs,
        "preambles": preambles,
        "checks": checks,
        "final": final,
        "duration_ms": int((time.monotonic() - started) * 1000),
    }


class handler(BaseHTTPRequestHandler):
    def _send(self, code: int, payload: dict[str, Any]) -> None:
        data = _body(payload)
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _auth(self) -> bool:
        auth = self.headers.get("Authorization") or ""
        scheme, _, token = auth.partition(" ")
        if scheme.lower() != "bearer" or token.count(".") != 2:
            return False
        try:
            validate_github_oidc_token(token)
            return True
        except GitHubOIDCError:
            return False

    def do_POST(self) -> None:
        if not self._auth():
            self._send(401, {"ok": False, "error": "unauthorized"})
            return
        try:
            payload = _finish()
            self._send(200 if payload.get("ok") else 409, payload)
        except Exception as exc:
            self._send(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:1000]})

    def do_GET(self) -> None:
        if not self._auth():
            self._send(401, {"ok": False, "error": "unauthorized"})
            return
        self._send(200, {"ok": True, "status": _snapshot()})
