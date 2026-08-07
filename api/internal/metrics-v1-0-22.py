from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler
from typing import Any

from bot import metric_sheet, metrics, sheet_preambles, sheets
from bot.config import get_settings
from bot.github_oidc import GitHubOIDCError, validate_github_oidc_token


CRITICAL_TABS = ("Videos", "ЧМ 2026")
EXPECTED_VISIBLE_START = ["Работа авторов", "ЧМ 2026", "Метрики", "Монтаж — справочно"]


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
    settings = get_settings()
    service = sheets._service()
    spreadsheet_id = settings.google_sheets_spreadsheet_id
    before = {title: _numeric_ids(service, spreadsheet_id, title) for title in CRITICAL_TABS}

    sync_result = metrics.sync_youtube_metrics()
    # Always rebuild once more from the DB snapshot history with this request's
    # shared Sheets service, even if one YouTube item failed during the fetch.
    summary_count = metric_sheet.refresh_metric_summary(service=service)
    layout = sheet_preambles.normalize_all_existing_sheet_preambles(service=service)

    after = {title: _numeric_ids(service, spreadsheet_id, title) for title in CRITICAL_TABS}
    properties = sheets._sheet_properties(service, spreadsheet_id)
    raw = properties.get(metric_sheet.RAW_SHEET_NAME) or {}
    visible_order = [
        title
        for title, props in sorted(
            properties.items(), key=lambda item: int(item[1].get("index") or 0)
        )
        if title and not bool(props.get("hidden"))
    ]
    summary_values = (
        service.spreadsheets()
        .values()
        .get(
            spreadsheetId=spreadsheet_id,
            range=f"'{metric_sheet.SUMMARY_SHEET_NAME}'!A1:H1000",
        )
        .execute()
        .get("values", [])
    )
    header = summary_values[1] if len(summary_values) > 1 else []
    data_rows = summary_values[2:] if len(summary_values) > 2 else []
    video_cells = [str(row[0]).strip() for row in data_rows if row and str(row[0]).strip()]
    expected_count = metrics.approved_youtube_video_count()

    checks = {
        "critical_unchanged": all(before[title] == after[title] for title in CRITICAL_TABS),
        "summary_sheet_exists": metric_sheet.SUMMARY_SHEET_NAME in properties,
        "summary_count_matches_youtube_videos": summary_count == expected_count == len(video_cells),
        "one_row_per_video": len(video_cells) == len(set(video_cells)),
        "header_matches": header[: len(metric_sheet.SUMMARY_COLUMNS)] == metric_sheet.SUMMARY_COLUMNS,
        "raw_hidden": bool(raw.get("hidden")),
        "visible_order": visible_order[:4] == EXPECTED_VISIBLE_START,
        "one_line_descriptions": not layout.get("failures"),
    }
    ok = all(checks.values()) and not sync_result.missing_key
    return {
        "ok": ok,
        "checks": checks,
        "sync": sync_result.to_dict(),
        "summary_count": summary_count,
        "approved_youtube_count": expected_count,
        "critical_counts": {title: len(after[title]) for title in CRITICAL_TABS},
        "visible_order": visible_order,
        "raw_hidden": bool(raw.get("hidden")),
        "layout": {
            "filled_titles": layout.get("filled_titles"),
            "empty_titles": layout.get("empty_titles"),
            "actual_order": layout.get("actual_order"),
            "failures": layout.get("failures"),
        },
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
            self._send(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:900]})
            return
        self._send(200 if payload.get("ok") else 500, payload)
