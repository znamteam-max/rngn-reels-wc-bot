from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from bot import db
from bot.links import normalize_instagram, normalize_tiktok, normalize_youtube


PLATFORM_FIELDS = {
    "instagram": ("instagram_url", "instagram_id"),
    "youtube": ("youtube_url", "youtube_id"),
    "tiktok": ("tiktok_url", "tiktok_id"),
    "vk": ("vk_url", None),
}

_ALLOWED_HOSTS = {
    "instagram": ("instagram.com",),
    "youtube": ("youtube.com", "youtu.be"),
    "tiktok": ("tiktok.com",),
    "vk": ("vk.com", "vk.ru", "vkvideo.ru"),
}

_VK_CLIP_ID_RE = re.compile(r"(?:clip|video)(-?\d+_\d+)", re.I)


def _host_allowed(platform: str, url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == allowed or host.endswith(f".{allowed}") for allowed in _ALLOWED_HOSTS[platform])


def platform_identity(platform: str, url: str) -> str:
    platform = str(platform or "").strip().lower()
    url = str(url or "").strip()
    if platform not in PLATFORM_FIELDS:
        raise ValueError(f"unsupported platform: {platform}")
    if not url or not _host_allowed(platform, url):
        raise ValueError(f"invalid {platform} URL")
    if platform == "instagram":
        value = normalize_instagram(url).external_id or ""
    elif platform == "youtube":
        value = normalize_youtube(url).external_id or ""
    elif platform == "tiktok":
        value = normalize_tiktok(url).external_id or ""
    else:
        match = _VK_CLIP_ID_RE.search(url)
        value = match.group(1) if match else ""
    if not value:
        raise ValueError(f"cannot extract {platform} publication id")
    return value


def _same_existing(platform: str, existing_url: str, platform_id: str) -> bool:
    if not existing_url:
        return False
    try:
        return platform_identity(platform, existing_url) == platform_id
    except Exception:
        return False


def apply_manual_links(items: list[dict[str, Any]], *, actor_username: str = "github_actions") -> dict[str, Any]:
    if not isinstance(items, list) or not items:
        raise ValueError("manual_links must be a non-empty list")
    if len(items) > 50:
        raise ValueError("manual_links limit is 50")

    result: dict[str, Any] = {"requested": len(items), "applied": 0, "noop": 0, "rejected": 0, "details": []}
    with db.transaction() as conn:
        with conn.cursor() as cur:
            for raw in items:
                detail: dict[str, Any] = {}
                try:
                    video_id = int(raw.get("video_id"))
                    platform = str(raw.get("platform") or "").strip().lower()
                    url = str(raw.get("url") or "").strip()
                    platform_id = platform_identity(platform, url)
                    url_field, id_field = PLATFORM_FIELDS[platform]
                    detail.update({"video_id": video_id, "platform": platform, "platform_id": platform_id})

                    cur.execute("SELECT * FROM videos WHERE id=%s FOR UPDATE", (video_id,))
                    video = cur.fetchone()
                    if not video:
                        raise ValueError("video_not_found")
                    if str(video.get("status") or "") != "approved":
                        raise ValueError("video_not_approved")

                    existing_url = str(video.get(url_field) or "").strip()
                    existing_id = str(video.get(id_field) or "").strip() if id_field else ""
                    if existing_url:
                        if _same_existing(platform, existing_url, platform_id):
                            detail["status"] = "noop_same_publication"
                            result["noop"] += 1
                            result["details"].append(detail)
                            continue
                        raise ValueError("existing_url_conflict")
                    if existing_id and existing_id != platform_id:
                        raise ValueError("existing_id_conflict")

                    assignments = [f"{url_field}=%s", "updated_at=now()"]
                    params: list[Any] = [url]
                    if id_field:
                        assignments.insert(1, f"{id_field}=%s")
                        params.append(platform_id)
                    params.append(video_id)
                    cur.execute(f"UPDATE videos SET {', '.join(assignments)} WHERE id=%s", params)
                    db.log_event(
                        conn,
                        entity_type="video",
                        entity_id=video_id,
                        action="manual_publication_link_added",
                        actor_username=actor_username,
                        before_data={"platform": platform, "url": existing_url or None, "platform_id": existing_id or None},
                        after_data={"platform": platform, "url": url, "platform_id": platform_id},
                    )
                    detail["status"] = "applied"
                    detail["url"] = url
                    result["applied"] += 1
                except Exception as exc:
                    detail.setdefault("video_id", raw.get("video_id"))
                    detail.setdefault("platform", raw.get("platform"))
                    detail["status"] = "rejected"
                    detail["error"] = f"{type(exc).__name__}: {exc}"[:240]
                    result["rejected"] += 1
                result["details"].append(detail)
    return result
