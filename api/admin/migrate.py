from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler
from typing import Any
from urllib.parse import parse_qs, urlparse

from bot import august_aircut_backfill, db
from bot.config import get_settings
from bot.runtime_migrations import ensure_runtime_migrations


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")


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
        if self.headers.get("Authorization") != f"Bearer {settings.cron_secret}":
            self._send_json(401, {"ok": False, "error": "unauthorized"})
            return
        try:
            result = ensure_runtime_migrations(force=True)
        except Exception as exc:
            self._send_json(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]})
            return
        self._send_json(
            200,
            {"ok": True, "schema_version": db.current_schema_version(), "migration": result},
        )

    def do_GET(self) -> None:
        query = parse_qs(urlparse(self.path).query)
        if (query.get("aircut_backfill") or [""])[0] == august_aircut_backfill.ONE_TIME_KEY:
            mode = (query.get("mode") or ["preview"])[0]
            try:
                result = august_aircut_backfill.run(mode)
            except ValueError as exc:
                self._send_json(400, {"ok": False, "error": str(exc)})
                return
            except Exception as exc:
                self._send_json(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]})
                return
            self._send_json(200, result)
            return
        self.do_POST()

    def do_HEAD(self) -> None:
        self.do_GET()
