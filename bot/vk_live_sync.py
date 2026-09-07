from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from bot import multiplatform_metrics, vk_direct_metrics


def sync_live_vk_clips(videos: list[dict[str, Any]]) -> dict[str, Any]:
    by_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for video in videos:
        if str(video.get("project_code") or "") == "world_cup_2026":
            continue
        url = str(video.get("vk_url") or "").strip()
        identity = vk_direct_metrics.parse_vk_clip_identity(url)
        if identity:
            by_id[identity].append(video)

    result: dict[str, Any] = {
        "videos": sum(len(items) for items in by_id.values()),
        "publications": len(by_id),
        "success": 0,
        "missing": 0,
        "conflicts": 0,
        "errors": 0,
        "views": 0,
        "details": [],
    }
    captured_at = datetime.now(timezone.utc)
    for identity, items in by_id.items():
        if len(items) != 1:
            result["conflicts"] += 1
            result["details"].append({"vk_id": identity, "status": "conflict", "video_ids": [int(item["id"]) for item in items]})
            continue
        video = items[0]
        try:
            stats = vk_direct_metrics.fetch_vk_clip_statistics(identity)
            if stats.views is None:
                result["missing"] += 1
                result["details"].append({"vk_id": identity, "video_id": int(video["id"]), "status": "missing_views"})
                continue
            multiplatform_metrics._upsert_snapshot(
                video,
                platform="vk",
                platform_video_id=identity,
                platform_url=str(video.get("vk_url") or "") or None,
                captured_at=captured_at,
                views=stats.views,
                likes=stats.likes,
                comments=stats.comments,
                shares=None,
                raw_data={"source": "vk_public_video", "item": stats.raw_data},
            )
            result["success"] += 1
            result["views"] += int(stats.views or 0)
            result["details"].append({"vk_id": identity, "video_id": int(video["id"]), "status": "ok", "views": stats.views})
        except Exception as exc:
            result["errors"] += 1
            result["details"].append({
                "vk_id": identity,
                "video_id": int(video["id"]),
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}"[:240],
            })
    return result
