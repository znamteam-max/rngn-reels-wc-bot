from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
from typing import Any

from bot.config import get_settings, missing_env_names, optional_missing_env_names
from bot import admin_tools, db, jobs
from bot.public_patch import handle_update, record_system_log
from bot.version import REQUIRED_SCHEMA_VERSION, VERSION
from bot.worker_kick import kick_worker_if_ready


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _safe_exception_detail(exc: Exception) -> str:
    detail = f"{type(exc).__name__}: {exc}"
    token = get_settings().bot_token
    if token:
        detail = detail.replace(token, "[token]")
    return detail[:500]


def _kick_worker_safely(reason: str) -> None:
    try:
        kick_worker_if_ready(reason=reason)
    except Exception:
        pass


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
                "version": VERSION,
                "required_schema_version": REQUIRED_SCHEMA_VERSION,
                "schema_ready": db.schema_version_applied(REQUIRED_SCHEMA_VERSION),
                "current_schema_version": db.current_schema_version(),
                "time": datetime.now(timezone.utc).isoformat(),
                "missing_env": missing_env_names(),
                "optional_missing_env": optional_missing_env_names(),
            },
        )

    def do_POST(self) -> None:
        started = time.monotonic()
        settings = get_settings()
        if not settings.webhook_secret:
            self._send_json(500, {"ok": False, "error": "WEBHOOK_SECRET is not configured"})
            return

        actual_secret = self.headers.get("X-Telegram-Bot-Api-Secret-Token")
        if actual_secret != settings.webhook_secret:
            self._send_json(401, {"ok": False, "error": "unauthorized"})
            return

        if not db.schema_version_applied(REQUIRED_SCHEMA_VERSION):
            self._send_json(
                503,
                {
                    "ok": False,
                    "error": "schema migration required",
                    "required_schema_version": REQUIRED_SCHEMA_VERSION,
                    "current_schema_version": db.current_schema_version(),
                },
            )
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

        update_id = update.get("update_id")
        try:
            claim = jobs.claim_telegram_update(update)
        except (TypeError, ValueError):
            self._send_json(400, {"ok": False, "error": "invalid update_id"})
            return
        except Exception as exc:
            detail = _safe_exception_detail(exc)
            self._send_json(503, {"ok": False, "error": "update claim failed", "detail": detail})
            return

        try:
            record_system_log(
                "webhook_received",
                "telegram_update",
                int(update_id),
                {"claim": claim},
            )
        except Exception:
            pass

        if claim.startswith("duplicate"):
            _kick_worker_safely("webhook_duplicate_tail")
            duration_ms = int((time.monotonic() - started) * 1000)
            try:
                record_system_log(
                    "webhook_duplicate",
                    "telegram_update",
                    int(update_id),
                    {"claim": claim, "duration_ms": duration_ms},
                )
            except Exception:
                pass
            self._send_json(200, {"ok": True, "duplicate": True})
            return

        try:
            handled = False
            message = update.get("message")
            if isinstance(message, dict):
                handled = admin_tools.handle_message(message)
            if not handled:
                handle_update(update)
            jobs.finish_telegram_update(int(update_id))
            _kick_worker_safely("webhook_tail")
        except Exception as exc:
            detail = _safe_exception_detail(exc)
            try:
                jobs.finish_telegram_update(int(update_id), error=detail)
            except Exception:
                pass
            duration_ms = int((time.monotonic() - started) * 1000)
            try:
                record_system_log(
                    "webhook_failed",
                    "telegram_update",
                    int(update_id),
                    {"error": detail, "duration_ms": duration_ms},
                )
                if duration_ms > 2500:
                    record_system_log(
                        "webhook_slow",
                        "telegram_update",
                        int(update_id),
                        {"duration_ms": duration_ms, "failed": True},
                    )
            except Exception:
                pass
            self._send_json(200, {"ok": False, "error": "handler failed", "detail": detail})
            return

        duration_ms = int((time.monotonic() - started) * 1000)
        try:
            record_system_log(
                "webhook_done",
                "telegram_update",
                int(update_id),
                {"duration_ms": duration_ms},
            )
            if duration_ms > 2500:
                record_system_log(
                    "webhook_slow",
                    "telegram_update",
                    int(update_id),
                    {"duration_ms": duration_ms},
                )
        except Exception:
            pass
        self._send_json(200, {"ok": True, "duration_ms": duration_ms})