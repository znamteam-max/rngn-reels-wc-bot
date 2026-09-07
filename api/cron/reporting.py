from __future__ import annotations

import hmac
import json
from http.server import BaseHTTPRequestHandler
from typing import Any

from bot import admin_tools, payment_policy, reporting_sheet_style, reporting_sheet_v2
from bot.config import get_settings
from bot.github_oidc import GitHubOIDCError, validate_github_oidc_token


payment_policy.install()


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


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
            return "vercel_cron"
        if token.count(".") != 2:
            return None
        try:
            validate_github_oidc_token(token)
        except GitHubOIDCError:
            return None
        return "github_actions"

    def do_GET(self) -> None:
        source = self._authenticate()
        if not source:
            self._send_json(401, {"ok": False, "error": "unauthorized"})
            return
        try:
            result = admin_tools.sync_reporting_sheets()
            v2 = reporting_sheet_v2.sync(admin_tools._active_videos())
            layout = reporting_sheet_style.finalize_layout()
        except Exception as exc:
            self._send_json(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]})
            return
        self._send_json(
            200,
            {
                "ok": True,
                "source": source,
                **result,
                "reporting_v2": v2,
                "reporting_layout": layout,
            },
        )

    def do_POST(self) -> None:
        self.do_GET()

    def do_HEAD(self) -> None:
        self.do_GET()
