from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler
from typing import Any
from urllib.parse import parse_qs, urlparse

from bot import job_worker, reconciliation


RUN_ID = 1


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _status_payload() -> dict[str, Any]:
    run = reconciliation.get_run(RUN_ID)
    if not run:
        return {
            "ok": False,
            "run_id": RUN_ID,
            "error": "run_not_found",
            "work_chat_id_present": bool(os.getenv("WORK_CHAT_ID")),
        }
    summary = run.get("summary") if isinstance(run.get("summary"), dict) else {}
    return {
        "ok": True,
        "run_id": RUN_ID,
        "status": run.get("status"),
        "stage": run.get("stage"),
        "mode": run.get("mode"),
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
        "safe_project_backfill_candidates": int(
            run.get("safe_project_backfill_candidates") or 0
        ),
        "conflicting_project_assignments": int(
            run.get("conflicting_project_assignments") or 0
        ),
        "mismatch_count": int(summary.get("mismatch_count") or reconciliation.run_mismatch_count(run)),
        "changed_project_ids": list(summary.get("changed_project_ids") or []),
        "db_project_counts": summary.get("db_project_counts") or {},
        "db_month_counts": summary.get("db_month_counts") or {},
        "month_sheet_counts": summary.get("month_sheet_counts") or {},
        "project_sheet_counts": summary.get("project_sheet_counts") or {},
        "rebuilt_sheet_names": list(summary.get("rebuilt_sheet_names") or []),
        "unfinished_request_count": int(summary.get("unfinished_request_count") or 0),
        "stale_session_count": int(summary.get("stale_session_count") or 0),
        "last_error": run.get("last_error"),
        "work_chat_id_present": bool(os.getenv("WORK_CHAT_ID")),
        "updated_at": run.get("updated_at"),
        "finished_at": run.get("finished_at"),
    }


class handler(BaseHTTPRequestHandler):
    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(
            _json_safe(payload), ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        action = (parse_qs(urlparse(self.path).query).get("action") or ["status"])[0]
        try:
            if action == "status":
                self._send_json(200, _status_payload())
                return
            if action == "confirm":
                run = reconciliation.get_run(RUN_ID)
                if not run:
                    self._send_json(404, _status_payload())
                    return
                if run.get("status") == "awaiting_confirmation":
                    confirmed = reconciliation.confirm_run(
                        RUN_ID,
                        mode="safe_backfill",
                        actor_tg_id=0,
                        actor_username="assistant_authorized_by_user",
                    )
                else:
                    confirmed = False
                payload = _status_payload()
                payload["confirmed_now"] = confirmed
                self._send_json(200, payload)
                return
            if action == "drain":
                result = job_worker.process_jobs(source="manual")
                self._send_json(200, {"ok": True, "worker": result, "run": _status_payload()})
                return
            self._send_json(400, {"ok": False, "error": "unsupported_action"})
        except Exception as exc:
            self._send_json(
                500,
                {
                    "ok": False,
                    "error": type(exc).__name__,
                    "run": _status_payload(),
                },
            )

    def do_POST(self) -> None:
        self.do_GET()
