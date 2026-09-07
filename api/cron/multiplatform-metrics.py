from __future__ import annotations

import hmac
import json
from http.server import BaseHTTPRequestHandler
from typing import Any

from bot import (
    cross_platform_discovery,
    manual_publication_links,
    multiplatform_metrics,
    short_tiktok_metrics,
    title_cross_platform_discovery,
    vk_live_sync,
)
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

    def _read_json(self) -> dict[str, Any]:
        length_text = self.headers.get("Content-Length") or "0"
        try:
            length = int(length_text)
        except ValueError:
            raise ValueError("invalid Content-Length")
        if length <= 0:
            return {}
        if length > 100_000:
            raise ValueError("request body too large")
        raw = self.rfile.read(length)
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object")
        return payload

    def _run(self, payload: dict[str, Any] | None = None) -> None:
        source = self._authenticate()
        if not source:
            self._send_json(401, {"ok": False, "error": "unauthorized"})
            return
        payload = payload or {}
        manual_result: dict[str, Any] | None = None
        try:
            manual_links = payload.get("manual_links")
            if manual_links is not None:
                if source != "github_actions":
                    self._send_json(403, {"ok": False, "error": "manual_links require GitHub OIDC"})
                    return
                manual_result = manual_publication_links.apply_manual_links(
                    manual_links,
                    actor_username="github_actions",
                )

            approved = multiplatform_metrics._approved_videos()
            title_discovery = title_cross_platform_discovery.refresh(approved)
            # Title discovery can fill links from Content Core videos-v2. Reload
            # before the older caption fallback so it only sees truly missing platforms.
            approved = multiplatform_metrics._approved_videos()
            discovery = cross_platform_discovery.refresh(approved)
            # Discovery can fill previously missing URLs/IDs, so reload the rows
            # before resolving share links and running exact/live metrics matchers.
            approved = multiplatform_metrics._approved_videos()
            short_tiktok = short_tiktok_metrics.refresh(approved)
            approved = multiplatform_metrics._approved_videos()
            vk_live = vk_live_sync.sync_live_vk_clips(approved)
            result = multiplatform_metrics.sync_multiplatform_metrics()
        except Exception as exc:
            self._send_json(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:500]})
            return
        result["source"] = source
        result["title_discovery"] = title_discovery
        result["discovery"] = discovery
        result["short_tiktok"] = short_tiktok
        result["vk_live"] = vk_live
        if manual_result is not None:
            result["manual_links"] = manual_result
        self._send_json(200, result)

    def do_GET(self) -> None:
        self._run()

    def do_POST(self) -> None:
        try:
            payload = self._read_json()
        except Exception as exc:
            self._send_json(400, {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]})
            return
        self._run(payload)

    def do_HEAD(self) -> None:
        self._run()
