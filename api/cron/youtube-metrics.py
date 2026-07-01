from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler
from typing import Any

from bot import metrics
from bot.config import get_settings


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

    def do_GET(self) -> None:
        settings = get_settings()
        if not settings.cron_secret:
            self._send_json(500, {"ok": False, "error": "CRON_SECRET not configured"})
            return
        if self.headers.get("Authorization") != f"Bearer {settings.cron_secret}":
            self._send_json(401, {"ok": False, "error": "unauthorized"})
            return

        try:
            result = metrics.sync_youtube_metrics()
        except Exception as exc:
            self._send_json(500, {"ok": False, "error": str(exc)[:300]})
            return

        self._send_json(200, result.to_dict())

    def do_HEAD(self) -> None:
        self.do_GET()
