from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests


YOUTUBE_VIDEOS_ENDPOINT = "https://www.googleapis.com/youtube/v3/videos"


class YouTubeAPIError(RuntimeError):
    def __init__(self, description: str, status_code: int | None = None) -> None:
        super().__init__(description)
        self.description = description
        self.status_code = status_code


@dataclass(frozen=True)
class YouTubeStats:
    video_id: str
    views: int | None
    likes: int | None
    comments: int | None
    raw_data: dict[str, Any]


def _int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_youtube_statistics_response(
    requested_ids: list[str],
    data: dict[str, Any],
) -> tuple[dict[str, YouTubeStats], list[str]]:
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        items = []

    stats_by_id: dict[str, YouTubeStats] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        video_id = item.get("id")
        if not video_id:
            continue
        statistics = item.get("statistics") or {}
        stats_by_id[str(video_id)] = YouTubeStats(
            video_id=str(video_id),
            views=_int_or_none(statistics.get("viewCount")),
            likes=_int_or_none(statistics.get("likeCount")),
            comments=_int_or_none(statistics.get("commentCount")),
            raw_data=item,
        )

    missing = [video_id for video_id in requested_ids if video_id not in stats_by_id]
    return stats_by_id, missing


def fetch_youtube_statistics(
    video_ids: list[str],
    api_key: str,
) -> tuple[dict[str, YouTubeStats], list[str]]:
    cleaned_ids = [video_id for video_id in video_ids if video_id]
    if not cleaned_ids:
        return {}, []
    if len(cleaned_ids) > 50:
        raise ValueError("YouTube API supports at most 50 video IDs per request")

    response = requests.get(
        YOUTUBE_VIDEOS_ENDPOINT,
        params={
            "part": "statistics",
            "id": ",".join(cleaned_ids),
            "key": api_key,
        },
        timeout=20,
    )
    try:
        data = response.json()
    except ValueError as exc:
        raise YouTubeAPIError("YouTube API returned a non-JSON response", response.status_code) from exc

    if not response.ok:
        error = data.get("error") if isinstance(data, dict) else None
        description = "YouTube API request failed"
        if isinstance(error, dict):
            description = str(error.get("message") or description)
        raise YouTubeAPIError(description, response.status_code)

    return parse_youtube_statistics_response(cleaned_ids, data)
