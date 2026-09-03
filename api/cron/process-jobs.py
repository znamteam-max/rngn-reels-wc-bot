from __future__ import annotations

import hmac
import json
from http.server import BaseHTTPRequestHandler
from typing import Any

from bot import (
    content_core_attribution,
    content_core_integration,
    content_core_multicore,
    project_workflow_patch,
)
from bot.config import get_settings
from bot.github_oidc import GitHubOIDCError, validate_github_oidc_token
from bot.job_worker import process_jobs


project_workflow_patch.install()
content_core_multicore.install()
content_core_attribution.install()
content_core_integration.install_worker()


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

    def _authenticate(self) -> str | None:
        settings = get_settings()
        authorization = self.headers.get("Authorization") or ""
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token:
            return None
        if settings.cron_secret and hmac.compare_digest(token, settings.cron_secret):
            user_agent = (self.headers.get("User-Agent") or "").lower()
            requested_source = (self.headers.get("X-Worker-Source") or "").lower()
            if requested_source == "event_kick" and user_agent.startswith("rngn-event-kick/1.0"):
                return "event_kick"
            return "vercel_cron" if "vercel-cron" in user_agent else "manual"
        if token.count(".") != 2:
            return None
        try:
            validate_github_oidc_token(token)
        except GitHubOIDCError:
            return None
        return "github_actions"

    def _run(self) -> None:
        settings = get_settings()
        source = self._authenticate()
        if not source:
            self._send_json(401, {"ok": False, "error": "unauthorized"})
            return
        if not settings.background_jobs_enabled:
            self._send_json(503, {"ok": False, "error": "background jobs disabled"})
            return
        try:
            closeout = None
            if source == "github_actions":
                from bot.world_cup_finalize import finalize_world_cup_2026

                closeout = finalize_world_cup_2026()
            result = process_jobs(source=source)
            if closeout is not None:
                result["world_cup_finalize"] = closeout
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
