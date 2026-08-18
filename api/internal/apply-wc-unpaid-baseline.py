from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler
from typing import Any

from bot.author_reports import initialize_world_cup_unpaid_baseline
from bot.github_oidc import GitHubOIDCError, validate_github_oidc_token


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

    def _authorized(self) -> bool:
        authorization = self.headers.get("Authorization") or ""
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token:
            return False
        try:
            validate_github_oidc_token(token)
        except GitHubOIDCError:
            return False
        return True

    def do_POST(self) -> None:
        if not self._authorized():
            self._send_json(401, {"ok": False, "error": "unauthorized"})
            return
        try:
            result = initialize_world_cup_unpaid_baseline()
        except Exception as exc:
            self._send_json(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:500]})
            return
        self._send_json(200, {"ok": True, **result})

    def do_GET(self) -> None:
        self._send_json(405, {"ok": False, "error": "method not allowed"})

    def do_HEAD(self) -> None:
        self.do_GET()
