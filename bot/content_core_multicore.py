from __future__ import annotations

from typing import Any

from bot import db
from bot import content_core_integration as core


_INSTALLED = False


def _publication_links_by_platform(video_id: int) -> dict[str, list[str]]:
    rows = db.fetch_all(
        """
        SELECT platform, content_core_publication_id
        FROM content_core_publication_links
        WHERE video_id = %s
        ORDER BY platform, content_core_publication_id
        """,
        (int(video_id),),
    )
    result: dict[str, list[str]] = {}
    for row in rows:
        platform = str(row.get("platform") or "").strip()
        publication_id = str(row.get("content_core_publication_id") or "").strip()
        if not platform or not publication_id:
            continue
        result.setdefault(platform, [])
        if publication_id not in result[platform]:
            result[platform].append(publication_id)
    return result


def install() -> None:
    """Treat Core technical-project publications as one bot production item.

    Content Core intentionally keeps some channels in separate technical projects.
    When its attach endpoint rejects a cross-project canonical merge, the reporting
    bot may still link those existing publications to one approved production item
    without mutating Content Core identity.
    """

    global _INSTALLED
    if _INSTALLED:
        return

    original_sync = core.sync_approved_video

    def sync_approved_video(video_id: int) -> dict[str, Any]:
        try:
            return original_sync(video_id)
        except core.ContentCoreConflict as exc:
            if "another project" not in str(exc).casefold():
                raise

            video = core._load_video(video_id)
            if not video:
                raise
            core._refresh_publication_links(video)
            publication_links = _publication_links_by_platform(video_id)
            submitted_platforms = {
                platform for platform, _ in core._submitted_urls(video)
            }
            ambiguous = {
                platform: ids
                for platform, ids in publication_links.items()
                if len(ids) > 1
            }
            if ambiguous or not submitted_platforms.issubset(publication_links):
                raise

            link = db.fetch_one(
                """
                SELECT content_core_video_id, matched_by_platform, details
                FROM content_core_video_links
                WHERE video_id = %s
                """,
                (int(video_id),),
            ) or {}
            details = dict(link.get("details") or {})
            details["publication_links"] = publication_links
            details["technical_project_mode"] = True
            details["technical_project_reason"] = core._safe_error(exc)

            core._set_link_state(
                int(video_id),
                status="resolved_multi_core",
                core_video_id=link.get("content_core_video_id"),
                matched_by_platform=link.get("matched_by_platform"),
                details=details,
                resolved=True,
            )
            with db.transaction() as conn:
                db.log_event(
                    conn,
                    entity_type="content_core_link",
                    entity_id=int(video_id),
                    action="content_core_resolved_multi_core",
                    actor_username="system",
                    after_data={
                        "content_core_video_id": link.get("content_core_video_id"),
                        "publication_links": publication_links,
                        "technical_project_mode": True,
                    },
                )
            return {
                "status": "resolved_multi_core",
                "video_id": int(video_id),
                "content_core_video_id": link.get("content_core_video_id"),
                "matched_by_platform": link.get("matched_by_platform"),
                "publication_links": publication_links,
            }

    core.sync_approved_video = sync_approved_video
    _INSTALLED = True
