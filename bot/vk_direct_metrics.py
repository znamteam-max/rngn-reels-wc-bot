from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

import requests


VK_CLIP_ID_RE = re.compile(r"(?:clip|video)(-?\d+_\d+)", re.I)
_EMBED_MARKER = "Object.assign(window.cur || {}, "


@dataclass(frozen=True)
class VkClipStats:
    views: int | None
    likes: int | None
    comments: int | None
    raw_data: dict[str, Any]


def parse_vk_clip_identity(url: str) -> str:
    match = VK_CLIP_ID_RE.search(str(url or ""))
    return match.group(1) if match else ""


def _count(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, dict):
        value = value.get("count")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _player_payload(text: str) -> dict[str, Any]:
    pos = text.find(_EMBED_MARKER)
    if pos < 0:
        raise ValueError("VK player payload marker not found")
    start = pos + len(_EMBED_MARKER)
    payload, _ = json.JSONDecoder().raw_decode(text[start:])
    if not isinstance(payload, dict):
        raise ValueError("VK player payload is not an object")
    return payload


def extract_stats_from_html(text: str, expected_identity: str) -> VkClipStats:
    payload = _player_payload(text)
    owner_text, video_text = expected_identity.split("_", 1)
    owner_id = int(owner_text)
    video_id = int(video_text)
    cache = payload.get("apiPrefetchCache") or []
    for entry in cache:
        if not isinstance(entry, dict) or entry.get("method") != "video.get":
            continue
        response = entry.get("response") or {}
        items = response.get("items") or [] if isinstance(response, dict) else []
        for item in items:
            if not isinstance(item, dict):
                continue
            item_owner = _count(item.get("owner_id"))
            item_id = _count(item.get("id"))
            if item_owner is not None and item_owner != owner_id:
                continue
            if item_id is not None and item_id != video_id:
                continue
            views = _count(item.get("views"))
            likes = _count(item.get("likes"))
            comments = _count(item.get("comments"))
            if views is None:
                raise ValueError("VK clip has no views counter")
            return VkClipStats(
                views=views,
                likes=likes,
                comments=comments,
                raw_data={
                    "owner_id": item.get("owner_id"),
                    "id": item.get("id"),
                    "date": item.get("date"),
                    "duration": item.get("duration"),
                    "description": str(item.get("description") or "")[:500],
                    "views": item.get("views"),
                    "likes": item.get("likes"),
                    "comments": item.get("comments"),
                },
            )
    raise ValueError("VK clip item not found in player payload")


def fetch_vk_clip_statistics(identity: str, *, timeout: int = 30) -> VkClipStats:
    if not re.fullmatch(r"-?\d+_\d+", str(identity or "")):
        raise ValueError("invalid VK clip identity")
    owner_id, video_id = identity.split("_", 1)
    url = f"https://vkvideo.ru/video_ext.php?oid={owner_id}&id={video_id}"
    response = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout)
    response.raise_for_status()
    return extract_stats_from_html(response.text, identity)
