from __future__ import annotations

from datetime import date, datetime
from typing import Any

import requests

from bot import content_core_integration as core


_INSTALLED = False


def _attribution_url() -> str:
    prefix, token = core._bridge_parts()
    return f"{prefix}/editor/{token}/production-attribution.tsv"


def _iso(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value or "").strip()
    return text or None


def _credit_payload(video: dict[str, Any], role: str) -> dict[str, str] | None:
    name = str(video.get(f"{role}_name") or "").strip()
    if not name:
        return None
    payload = {
        "source_system": "rngn-reels-wc-bot",
        "source_item_id": str(video["id"]),
        "credit_key": role,
        "role": role,
        "person_name": name,
    }
    username = str(video.get(f"{role}_username") or "").strip()
    tg_id = str(video.get(f"{role}_tg_id") or "").strip()
    project_code = str(video.get("project_code") or "").strip()
    approved_at = _iso(video.get("checked_at"))
    if username:
        payload["person_username"] = username.lstrip("@")
    if tg_id:
        payload["person_tg_id"] = tg_id
    if project_code:
        payload["project_code"] = project_code
    if approved_at:
        payload["approved_at"] = approved_at
    return payload


def _sync_attributions(core_video_id: str, video: dict[str, Any]) -> int:
    saved = 0
    for role in ("author", "montage", "voice"):
        payload = _credit_payload(video, role)
        if not payload:
            continue
        payload["video"] = core_video_id
        response = requests.get(_attribution_url(), params=payload, timeout=30)
        text = response.text.strip()
        if not response.ok or not text.startswith("OK:"):
            raise RuntimeError(
                f"Content Core attribution failed for {role}: "
                f"HTTP {response.status_code} {core._safe_error(text)}"
            )
        saved += 1
    return saved


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    original_sync = core.sync_approved_video

    def sync_approved_video(video_id: int) -> dict[str, Any]:
        result = original_sync(video_id)
        core_video_id = str(result.get("content_core_video_id") or "").strip()
        if not core_video_id:
            return result
        video = core._load_video(video_id)
        if not video:
            return result
        result = dict(result)
        result["production_attributions"] = _sync_attributions(core_video_id, video)
        return result

    core.sync_approved_video = sync_approved_video
    _INSTALLED = True
