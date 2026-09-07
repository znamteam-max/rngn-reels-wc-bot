from __future__ import annotations

import hmac
import json
from http.server import BaseHTTPRequestHandler
from typing import Any

from bot import multiplatform_metrics, short_tiktok_metrics
from bot.config import get_settings
from bot.github_oidc import GitHubOIDCError, validate_github_oidc_token


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


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

    def _authenticate(self) -> str | None:
        settings = get_settings()
        authorization = self.headers.get("Authorization") or ""
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token:
            return None
        if settings.cron_secret and hmac.compare_digest(token, settings.cron_secret):
            return "cron"
        if token.count(".") != 2:
            return None
        try:
            validate_github_oidc_token(token)
        except GitHubOIDCError:
            return None
        return "github_actions"

    def _run(self) -> None:
        source = self._authenticate()
        if not source:
            self._send_json(401, {"ok": False, "error": "unauthorized"})
            return
        try:
            approved = multiplatform_metrics._approved_videos()
            short_tiktok = short_tiktok_metrics.refresh(approved)
            result = multiplatform_metrics.sync_multiplatform_metrics()
        except Exception as exc:
            self._send_json(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:500]})
            return
        result["source"] = source
        result["short_tiktok"] = short_tiktok
        self._send_json(200, result)

    def do_GET(self) -> None:
        self._run()

    def do_POST(self) -> None:
        self._run()

    def do_HEAD(self) -> None:
        self._run()
