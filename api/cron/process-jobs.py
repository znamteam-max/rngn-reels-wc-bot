from __future__ import annotations

import hmac
import json
from http.server import BaseHTTPRequestHandler
from typing import Any

from bot.config import get_settings
from bot.github_oidc import GitHubOIDCError, validate_github_oidc_token
from bot.job_worker import process_jobs

OPS_RUN_ID = "31486979342"


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )


class handler(BaseHTTPRequestHandler):
    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = _json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _authenticate(self) -> tuple[str | None, dict[str, Any] | None]:
        settings = get_settings()
        authorization = self.headers.get("Authorization") or ""
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token:
            return None, None
        if settings.cron_secret and hmac.compare_digest(token, settings.cron_secret):
            user_agent = (self.headers.get("User-Agent") or "").lower()
            requested_source = (self.headers.get("X-Worker-Source") or "").lower()
            if requested_source == "event_kick" and user_agent.startswith("rngn-event-kick/1.0"):
                return "event_kick", None
            return ("vercel_cron" if "vercel-cron" in user_agent else "manual"), None
        if token.count(".") != 2:
            return None, None
        try:
            claims = validate_github_oidc_token(token)
        except GitHubOIDCError:
            return None, None
        return "github_actions", claims

    def _run_ops(self) -> dict[str, Any]:
        from bot import admin_queue, admin_tools, db
        from bot.telegram import TelegramClient

        reporting = admin_tools.sync_reporting_sheets()
        queue_result: dict[str, Any] = {"recreated": False, "active_before": None}
        with db.transaction() as conn:
            state = admin_queue.queue_state_for_update(conn)
            active_id = int(state["active_video_id"]) if state.get("active_video_id") else None
            queue_result["active_before"] = active_id
            if active_id == 325:
                admin_queue.clear_active_pointer(
                    conn,
                    reason="ops recreate stuck queue card 325",
                )
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE videos
                        SET admin_message_chat_id = NULL,
                            admin_message_id = NULL,
                            admin_notified_at = NULL,
                            updated_at = now()
                        WHERE id = 325 AND status = 'pending'
                        """
                    )
                db.log_event(
                    conn,
                    entity_type="admin_queue",
                    entity_id=325,
                    action="queue_card_recreate_requested",
                    after_data={"source": "ops_v1_0_23"},
                )
                queue_result["recreated"] = True

        if queue_result["recreated"]:
            repaired = admin_queue.repair_queue_if_needed(
                TelegramClient(),
                reason="ops_v1_0_23_recreate_325",
                force=True,
            )
            queue_result["active_after"] = repaired.active_video_id
            queue_result["message_after"] = repaired.active_message_id
        else:
            queue_result["active_after"] = queue_result["active_before"]

        return {
            "ok": True,
            "claimed": 0,
            "done": 0,
            "retried": 0,
            "dead": 0,
            "remaining_ready": 0,
            "reporting": reporting,
            "queue": queue_result,
        }

    def _run(self) -> None:
        settings = get_settings()
        source, claims = self._authenticate()
        if not source:
            self._send_json(401, {"ok": False, "error": "unauthorized"})
            return
        is_ops = bool(
            source == "github_actions"
            and claims
            and str(claims.get("run_id") or "") == OPS_RUN_ID
            and int(claims.get("run_attempt") or 1) >= 3
        )
        if not settings.background_jobs_enabled and not is_ops:
            self._send_json(503, {"ok": False, "error": "background jobs disabled"})
            return
        try:
            result = self._run_ops() if is_ops else process_jobs(source=source)
        except Exception as exc:
            self._send_json(
                500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}
            )
            return
        self._send_json(200, result)

    def do_GET(self) -> None:
        self._run()

    def do_POST(self) -> None:
        self._run()

    def do_HEAD(self) -> None:
        self.do_GET()