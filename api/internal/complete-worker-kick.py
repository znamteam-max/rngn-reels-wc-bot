from __future__ import annotations

import hmac
import json
from http.server import BaseHTTPRequestHandler
from typing import Any

from bot.config import get_settings
from bot.worker_kick import complete_worker_kick


MAX_BODY_BYTES = 4_096


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

    def do_POST(self) -> None:
        settings = get_settings()
        if not settings.cron_secret:
            self._send_json(500, {"ok": False, "error": "CRON_SECRET not configured"})
            return
        authorization = self.headers.get("Authorization") or ""
        expected = f"Bearer {settings.cron_secret}"
        if not hmac.compare_digest(authorization, expected):
            self._send_json(401, {"ok": False, "error": "unauthorized"})
            return
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_json(400, {"ok": False, "error": "invalid content length"})
            return
        if content_length < 0 or content_length > MAX_BODY_BYTES:
            self._send_json(413, {"ok": False, "error": "payload too large"})
            return
        try:
            result = complete_worker_kick()
        except Exception as exc:
            self._send_json(500, {"ok": False, "error": type(exc).__name__})
            return
        self._send_json(200, result)

    def do_GET(self) -> None:
        self._send_json(405, {"ok": False, "error": "method not allowed"})

    def do_HEAD(self) -> None:
        self._send_json(405, {"ok": False, "error": "method not allowed"})
