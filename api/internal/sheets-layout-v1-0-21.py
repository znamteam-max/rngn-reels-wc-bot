from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler
from typing import Any

from bot import sheet_preambles, sheets
from bot.config import get_settings
from bot.github_oidc import GitHubOIDCError, validate_github_oidc_token


CRITICAL_TABS = ("Videos", "ЧМ 2026")


def _json(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")


def _numeric_ids(service, spreadsheet_id: str, title: str) -> list[int]:
    rows = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=f"'{title}'!A:A")
        .execute()
        .get("values", [])
    )
    result: list[int] = []
    for row in rows:
        if not row:
            continue
        value = str(row[0]).strip()
        if value.isdigit():
            result.append(int(value))
    return result


def _run() -> dict[str, Any]:
    service = sheets._service()
    spreadsheet_id = get_settings().google_sheets_spreadsheet_id
    before = {title: _numeric_ids(service, spreadsheet_id, title) for title in CRITICAL_TABS}
    layout = sheet_preambles.normalize_all_existing_sheet_preambles(service=service)
    after = {title: _numeric_ids(service, spreadsheet_id, title) for title in CRITICAL_TABS}
    critical_unchanged = {
        title: before[title] == after[title]
        for title in CRITICAL_TABS
    }
    ok = (
        not layout.get("failures")
        and layout.get("order_matches") is True
        and all(critical_unchanged.values())
    )
    return {
        "ok": ok,
        "critical_unchanged": critical_unchanged,
        "critical_counts": {title: len(after[title]) for title in CRITICAL_TABS},
        "filled_titles": layout.get("filled_titles"),
        "empty_titles": layout.get("empty_titles"),
        "actual_order": layout.get("actual_order"),
        "collapsed": layout.get("collapsed_three_row_descriptions"),
        "failures": layout.get("failures"),
    }


class handler(BaseHTTPRequestHandler):
    def _send(self, code: int, payload: dict[str, Any]) -> None:
        body = _json(payload)
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        auth = self.headers.get("Authorization") or ""
        scheme, _, token = auth.partition(" ")
        if scheme.lower() != "bearer" or token.count(".") != 2:
            return False
        try:
            validate_github_oidc_token(token)
            return True
        except GitHubOIDCError:
            return False

    def do_GET(self) -> None:
        if not self._authorized():
            self._send(401, {"ok": False, "error": "unauthorized"})
            return
        self._send(200, {"ok": True, "ready": True})

    def do_POST(self) -> None:
        if not self._authorized():
            self._send(401, {"ok": False, "error": "unauthorized"})
            return
        try:
            payload = _run()
        except Exception as exc:
            self._send(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:800]})
            return
        self._send(200 if payload.get("ok") else 500, payload)
