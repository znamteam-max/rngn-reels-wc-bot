from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler


class handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        checks: dict[str, str] = {}
        for name in ("psycopg", "psycopg_pool", "bot.config", "bot.db"):
            try:
                __import__(name)
                checks[name] = "ok"
            except Exception as exc:
                checks[name] = f"{type(exc).__name__}: {exc}"[:300]
        body = json.dumps({"ok": all(value == "ok" for value in checks.values()), "checks": checks}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
