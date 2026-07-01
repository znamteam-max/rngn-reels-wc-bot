from __future__ import annotations

import json
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
from typing import Any

from bot.config import get_settings, missing_env_names, optional_missing_env_names
from bot.public_patch import handle_update, record_system_log


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

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        self._send_json(
            200,
            {
                "ok": True,
                "service": "rngn-reels-wc-bot",
                "time": datetime.now(timezone.utc).isoformat(),
                "missing_env": missing_env_names(),
                "optional_missing_env": optional_missing_env_names(),
            },
        )

    def do_POST(self) -> None:
        settings = get_settings()
        if not settings.webhook_secret:
            self._send_json(500, {"ok": False, "error": "WEBHOOK_SECRET is not configured"})
            return

        actual_secret = self.headers.get("X-Telegram-Bot-Api-Secret-Token")
        if actual_secret != settings.webhook_secret:
            self._send_json(401, {"ok": False, "error": "unauthorized"})
            return

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_json(400, {"ok": False, "error": "invalid content length"})
            return
        if content_length <= 0 or content_length > 2_000_000:
            self._send_json(413, {"ok": False, "error": "invalid payload size"})
            return

        try:
            update = json.loads(self.rfile.read(content_length).decode("utf-8"))
        except ValueError:
            self._send_json(400, {"ok": False, "error": "invalid json"})
            return

        try:
            handle_update(update)
        except Exception as exc:
            record_system_log(
                "webhook_update_failed",
                "telegram_update",
                None,
                {"error": str(exc)[:500]},
            )
            self._send_json(200, {"ok": False, "error": "handler failed"})
            return

        self._send_json(200, {"ok": True})
