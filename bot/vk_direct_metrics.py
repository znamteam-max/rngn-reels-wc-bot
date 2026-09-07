from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

import requests


VK_CLIP_ID_RE = re.compile(r"(?:clip|video)(-?\d+_\d+)", re.I)
_ASSIGN_RE = re.compile(r"Object\.assign\(\s*window\.cur\s*\|\|\s*\{\}\s*,\s*", re.I)


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


def _decode_object_at(text: str, start: int) -> dict[str, Any] | None:
    try:
        payload, _ = json.JSONDecoder().raw_decode(text[start:])
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _player_payload(text: str) -> dict[str, Any]:
    # VK serves several HTML variants for the same clip. The historical page
    # used `Object.assign(window.cur || {}, {...})`, but newer responses can
    # change whitespace or assign the object directly. Accept all of them.
    for match in _ASSIGN_RE.finditer(text):
        payload = _decode_object_at(text, match.end())
        if payload and isinstance(payload.get("apiPrefetchCache"), list):
            return payload

    marker = '"apiPrefetchCache"'
    pos = text.find(marker)
    while pos >= 0:
        # In the direct-assignment variant the root `{` is immediately before
        # apiPrefetchCache. Try nearby opening braces from nearest to farthest.
        left = max(0, pos - 4096)
        brace_positions = [idx for idx in range(left, pos + 1) if text[idx] == "{"]
        for start in reversed(brace_positions):
            payload = _decode_object_at(text, start)
            if payload and isinstance(payload.get("apiPrefetchCache"), list):
                return payload
        pos = text.find(marker, pos + len(marker))

    raise ValueError("VK player payload marker not found")


def _stats_from_item(item: dict[str, Any], *, owner_id: int, video_id: int) -> VkClipStats | None:
    item_owner = _count(item.get("owner_id"))
    item_id = _count(item.get("id"))
    if item_owner is not None and item_owner != owner_id:
        return None
    if item_id is not None and item_id != video_id:
        return None
    views = _count(item.get("views"))
    if views is None:
        return None
    likes = _count(item.get("likes"))
    comments = _count(item.get("comments"))
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


def _fallback_stats_from_text(text: str, expected_identity: str) -> VkClipStats | None:
    """Recover counters from a VK video.get JSON fragment if page shell changed."""
    owner_text, video_text = expected_identity.split("_", 1)
    owner_id = int(owner_text)
    video_id = int(video_text)
    identity_tokens = (
        expected_identity,
        f'"videos":"{expected_identity}"',
        f'"owner_id":{owner_id}',
        f'"id":{video_id}',
    )
    anchor = -1
    for token in identity_tokens:
        anchor = text.find(token)
        if anchor >= 0:
            break
    if anchor < 0:
        return None

    left = max(0, anchor - 12000)
    right = min(len(text), anchor + 180000)
    fragment = text[left:right]
    owner_match = re.search(rf'"owner_id"\s*:\s*{re.escape(str(owner_id))}(?:\D|$)', fragment)
    id_match = re.search(rf'"id"\s*:\s*{re.escape(str(video_id))}(?:\D|$)', fragment)
    views_match = re.search(r'"views"\s*:\s*(\d+)', fragment)
    if not views_match or (not owner_match and not id_match):
        return None
    likes_match = re.search(r'"likes"\s*:\s*(?:\{[^{}]*?"count"\s*:\s*)?(\d+)', fragment)
    comments_match = re.search(r'"comments"\s*:\s*(?:\{[^{}]*?"count"\s*:\s*)?(\d+)', fragment)
    return VkClipStats(
        views=int(views_match.group(1)),
        likes=int(likes_match.group(1)) if likes_match else None,
        comments=int(comments_match.group(1)) if comments_match else None,
        raw_data={
            "owner_id": owner_id,
            "id": video_id,
            "views": int(views_match.group(1)),
            "likes": int(likes_match.group(1)) if likes_match else None,
            "comments": int(comments_match.group(1)) if comments_match else None,
            "parser": "html_fragment_fallback",
        },
    )


def extract_stats_from_html(text: str, expected_identity: str) -> VkClipStats:
    owner_text, video_text = expected_identity.split("_", 1)
    owner_id = int(owner_text)
    video_id = int(video_text)
    try:
        payload = _player_payload(text)
    except ValueError:
        fallback = _fallback_stats_from_text(text, expected_identity)
        if fallback is not None:
            return fallback
        raise

    cache = payload.get("apiPrefetchCache") or []
    for entry in cache:
        if not isinstance(entry, dict) or entry.get("method") != "video.get":
            continue
        response = entry.get("response") or {}
        items = response.get("items") or [] if isinstance(response, dict) else []
        for item in items:
            if not isinstance(item, dict):
                continue
            stats = _stats_from_item(item, owner_id=owner_id, video_id=video_id)
            if stats is not None:
                return stats

    fallback = _fallback_stats_from_text(text, expected_identity)
    if fallback is not None:
        return fallback
    raise ValueError("VK clip item not found in player payload")


def fetch_vk_clip_statistics(identity: str, *, timeout: int = 30) -> VkClipStats:
    if not re.fullmatch(r"-?\d+_\d+", str(identity or "")):
        raise ValueError("invalid VK clip identity")
    owner_id, video_id = identity.split("_", 1)
    urls = (
        f"https://vkvideo.ru/video_ext.php?oid={owner_id}&id={video_id}",
        f"https://vk.com/video{identity}",
        f"https://vk.ru/video{identity}",
    )
    errors: list[str] = []
    for url in urls:
        try:
            response = requests.get(
                url,
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=timeout,
                allow_redirects=True,
            )
            response.raise_for_status()
            return extract_stats_from_html(response.text, identity)
        except Exception as exc:
            errors.append(f"{url}: {type(exc).__name__}: {exc}")
    raise ValueError("VK clip metrics unavailable: " + " | ".join(errors)[:1200])
