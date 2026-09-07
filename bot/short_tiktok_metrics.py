from __future__ import annotations

import csv
import io
import re
from collections import defaultdict
from typing import Any
from urllib.parse import urlparse

import requests

from bot import db, multiplatform_metrics


VIDEO_MIRROR_SUFFIX = "/videos-v2.tsv"
DASHBOARD_VIDEOS_URL = "https://rngn-content-dashboard.rngn-znamteam.workers.dev/api/videos"
TIKTOK_URL_COLUMN = "TikTok URL"
PLATFORM_URL_COLUMNS = {
    "instagram": "Instagram URL",
    "youtube": "YouTube URL",
    "vk": "VK URL",
}
SHORT_TIKTOK_RE = re.compile(r"https?://(?:www\.)?tiktok\.com/t/([^/?#]+)", re.I)
TIKTOK_VIDEO_RE = re.compile(r"/video/(\d+)(?:[/?#]|$)", re.I)
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/152.0.0.0 Safari/537.36"
)

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


def _numeric_tiktok_id(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if host != "tiktok.com" and not host.endswith(".tiktok.com"):
        return ""
    match = TIKTOK_VIDEO_RE.search(parsed.path)
    return match.group(1) if match else ""


def _resolve_short_url(url: str) -> str:
    """Resolve an author-supplied TikTok share URL to its exact numeric video id.

    We only accept a final URL that remains on a TikTok hostname and contains
    the canonical /video/<digits> path. A final 403/429 is still acceptable for
    identity purposes because requests has already followed TikTok's redirect
    chain and response.url is the canonical location.
    """
    if not SHORT_TIKTOK_RE.search(url):
        return ""
    try:
        response = requests.get(
            url,
            headers={"User-Agent": USER_AGENT},
            allow_redirects=True,
            stream=True,
            timeout=20,
        )
        try:
            return _numeric_tiktok_id(str(response.url or ""))
        finally:
            response.close()
    except requests.RequestException:
        return ""


def platform_id(video: dict[str, Any], platform: str) -> str:
    direct = _ORIGINAL_PLATFORM_ID(video, platform)
    if direct or platform != "tiktok":
        return direct
    video_id = int(video.get("id") or 0)
    canonical = _CANONICAL_TIKTOK_BY_VIDEO_ID.get(video_id)
    if canonical:
        return canonical
    # A short TikTok share URL is still a supplied publication for coverage.
    # The metrics endpoint calls refresh() before matching, so this sentinel is
    # never persisted as a platform metric identifier.
    match = SHORT_TIKTOK_RE.search(_text(video.get("tiktok_url")))
    return f"short:{match.group(1)}" if match else ""


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    multiplatform_metrics._platform_id = platform_id
    _INSTALLED = True


def _core_row_id(row: dict[str, Any], platform: str) -> str:
    url = _text(row.get(PLATFORM_URL_COLUMNS[platform]))
    if not url:
        return ""
    probe = {f"{platform}_url": url, f"{platform}_id": ""}
    return _ORIGINAL_PLATFORM_ID(probe, platform)


def _core_tiktok_id(row: dict[str, Any]) -> str:
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


def _fetch_core_rows() -> tuple[list[dict[str, Any]], str]:
    """Read linked Core videos if direct TikTok resolution did not work."""
    mirror_error: Exception | None = None
    try:
        response = requests.get(_videos_v2_url(), timeout=45)
        response.raise_for_status()
        return list(csv.DictReader(io.StringIO(response.text), delimiter="\t")), "mirror"
    except requests.RequestException as exc:
        mirror_error = exc

    try:
        response = requests.get(DASHBOARD_VIDEOS_URL, timeout=45)
        response.raise_for_status()
        payload = response.json()
        rows = payload.get("rows") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise RuntimeError("Content Core dashboard /api/videos returned no rows")
        clean_rows = [row for row in rows if isinstance(row, dict)]
        if not clean_rows:
            raise RuntimeError("Content Core dashboard /api/videos returned empty rows")
        return clean_rows, "dashboard"
    except Exception as dashboard_error:
        if mirror_error is not None:
            raise RuntimeError(
                f"Content Core video lookup failed: mirror={type(mirror_error).__name__}; "
                f"dashboard={type(dashboard_error).__name__}"
            ) from dashboard_error
        raise


def _persist_resolved(resolved: dict[int, str]) -> int:
    if not resolved:
        return 0
    changed = 0
    with db.transaction() as conn:
        with conn.cursor() as cur:
            for video_id, tiktok_id in resolved.items():
                cur.execute(
                    """
                    UPDATE videos
                    SET tiktok_id = %s, updated_at = now()
                    WHERE id = %s
                      AND COALESCE(tiktok_id, '') = ''
                    """,
                    (tiktok_id, video_id),
                )
                changed += int(cur.rowcount or 0)
    return changed


def _resolve_from_core(
    videos: list[dict[str, Any]],
    rows: list[dict[str, Any]],
) -> tuple[dict[int, str], int, int]:
    rows_by_key: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        for platform in ("instagram", "youtube", "vk"):
            value = _core_row_id(row, platform)
            if value:
                rows_by_key[(platform, value)].append(row)

    resolved: dict[int, str] = {}
    ambiguous = 0
    unmatched = 0
    for video in videos:
        candidates: set[str] = set()
        for key in _known_keys(video):
            for row in rows_by_key.get(key, []):
                tiktok_id = _core_tiktok_id(row)
                if tiktok_id:
                    candidates.add(tiktok_id)
        if len(candidates) == 1:
            resolved[int(video["id"])] = next(iter(candidates))
        elif len(candidates) > 1:
            ambiguous += 1
        else:
            unmatched += 1
    return resolved, ambiguous, unmatched


def refresh(videos: list[dict[str, Any]]) -> dict[str, Any]:
    install()
    targets = [
        video
        for video in videos
        if _is_short_tiktok(video) and not _ORIGINAL_PLATFORM_ID(video, "tiktok")
    ]
    if not targets:
        _CANONICAL_TIKTOK_BY_VIDEO_ID.clear()
        return {
            "short_links": 0,
            "resolved": 0,
            "persisted": 0,
            "direct_resolved": 0,
            "core_resolved": 0,
            "ambiguous": 0,
            "unmatched": 0,
            "core_lookup_failed": 0,
            "source": "none",
        }

    resolved: dict[int, str] = {}
    remaining: list[dict[str, Any]] = []
    for video in targets:
        tiktok_id = _resolve_short_url(_text(video.get("tiktok_url")))
        if tiktok_id:
            resolved[int(video["id"])] = tiktok_id
        else:
            remaining.append(video)

    direct_resolved = len(resolved)
    core_resolved = 0
    ambiguous = 0
    unmatched = 0
    core_lookup_failed = 0
    core_source = ""

    if remaining:
        try:
            rows, core_source = _fetch_core_rows()
            core_matches, ambiguous, unmatched = _resolve_from_core(remaining, rows)
            resolved.update(core_matches)
            core_resolved = len(core_matches)
        except Exception:
            # Do not discard exact IDs already resolved from the submitted links
            # merely because Content Core's heavier read endpoints are degraded.
            core_lookup_failed = 1
            unmatched = len(remaining)

    _CANONICAL_TIKTOK_BY_VIDEO_ID.clear()
    _CANONICAL_TIKTOK_BY_VIDEO_ID.update(resolved)
    persisted = _persist_resolved(resolved)

    sources: list[str] = []
    if direct_resolved:
        sources.append("redirect")
    if core_resolved and core_source:
        sources.append(core_source)
    source = "+".join(sources) if sources else (core_source or "none")

    return {
        "short_links": len(targets),
        "resolved": len(resolved),
        "persisted": persisted,
        "direct_resolved": direct_resolved,
        "core_resolved": core_resolved,
        "ambiguous": ambiguous,
        "unmatched": unmatched,
        "core_lookup_failed": core_lookup_failed,
        "source": source,
    }
