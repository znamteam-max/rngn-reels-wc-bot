from __future__ import annotations

import csv
import io
import re
from collections import defaultdict
from typing import Any

import requests

from bot import multiplatform_metrics


VIDEO_MIRROR_SUFFIX = "/videos-v2.tsv"
TIKTOK_URL_COLUMN = "TikTok URL"
PLATFORM_URL_COLUMNS = {
    "instagram": "Instagram URL",
    "youtube": "YouTube URL",
    "vk": "VK URL",
}
SHORT_TIKTOK_RE = re.compile(r"https?://(?:www\.)?tiktok\.com/t/([^/?#]+)", re.I)

_ORIGINAL_PLATFORM_ID = multiplatform_metrics._platform_id
_INSTALLED = False
_CANONICAL_TIKTOK_BY_VIDEO_ID: dict[int, str] = {}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _videos_v2_url() -> str:
    bridge = multiplatform_metrics.CONTENT_CORE_BRIDGE_URL
    prefix, marker, rest = bridge.partition("/mirror/")
    if not marker or "/" not in rest:
        raise RuntimeError("Content Core bridge URL has unexpected format")
    token = rest.split("/", 1)[0]
    return f"{prefix.rstrip('/')}/mirror/{token}{VIDEO_MIRROR_SUFFIX}"


def _is_short_tiktok(video: dict[str, Any]) -> bool:
    return bool(SHORT_TIKTOK_RE.search(_text(video.get("tiktok_url"))))


def platform_id(video: dict[str, Any], platform: str) -> str:
    direct = _ORIGINAL_PLATFORM_ID(video, platform)
    if direct or platform != "tiktok":
        return direct
    video_id = int(video.get("id") or 0)
    canonical = _CANONICAL_TIKTOK_BY_VIDEO_ID.get(video_id)
    if canonical:
        return canonical
    # For reporting/coverage, a supplied short TikTok URL still counts as a
    # supplied publication even when a fresh serverless process has not loaded
    # the Content Core alias map. This sentinel is never written as a metric ID
    # because the metrics endpoint calls refresh() before matching.
    match = SHORT_TIKTOK_RE.search(_text(video.get("tiktok_url")))
    return f"short:{match.group(1)}" if match else ""


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    multiplatform_metrics._platform_id = platform_id
    _INSTALLED = True


def _core_row_id(row: dict[str, str], platform: str) -> str:
    url = _text(row.get(PLATFORM_URL_COLUMNS[platform]))
    if not url:
        return ""
    probe = {f"{platform}_url": url, f"{platform}_id": ""}
    return _ORIGINAL_PLATFORM_ID(probe, platform)


def _core_tiktok_id(row: dict[str, str]) -> str:
    url = _text(row.get(TIKTOK_URL_COLUMN))
    if not url:
        return ""
    probe = {"tiktok_url": url, "tiktok_id": ""}
    return _ORIGINAL_PLATFORM_ID(probe, "tiktok")


def _known_keys(video: dict[str, Any]) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    for platform in ("instagram", "youtube", "vk"):
        value = _ORIGINAL_PLATFORM_ID(video, platform)
        if value:
            keys.add((platform, value))
    return keys


def refresh(videos: list[dict[str, Any]]) -> dict[str, int]:
    install()
    targets = [
        video
        for video in videos
        if _is_short_tiktok(video) and not _ORIGINAL_PLATFORM_ID(video, "tiktok")
    ]
    if not targets:
        _CANONICAL_TIKTOK_BY_VIDEO_ID.clear()
        return {"short_links": 0, "resolved": 0, "ambiguous": 0, "unmatched": 0}

    response = requests.get(_videos_v2_url(), timeout=45)
    response.raise_for_status()
    rows = list(csv.DictReader(io.StringIO(response.text), delimiter="\t"))

    rows_by_key: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        for platform in ("instagram", "youtube", "vk"):
            value = _core_row_id(row, platform)
            if value:
                rows_by_key[(platform, value)].append(row)

    resolved: dict[int, str] = {}
    ambiguous = 0
    unmatched = 0
    for video in targets:
        candidates: dict[str, dict[str, str]] = {}
        for key in _known_keys(video):
            for row in rows_by_key.get(key, []):
                tiktok_id = _core_tiktok_id(row)
                if tiktok_id:
                    candidates[tiktok_id] = row
        if len(candidates) == 1:
            resolved[int(video["id"])] = next(iter(candidates))
        elif len(candidates) > 1:
            ambiguous += 1
        else:
            unmatched += 1

    _CANONICAL_TIKTOK_BY_VIDEO_ID.clear()
    _CANONICAL_TIKTOK_BY_VIDEO_ID.update(resolved)
    return {
        "short_links": len(targets),
        "resolved": len(resolved),
        "ambiguous": ambiguous,
        "unmatched": unmatched,
    }
