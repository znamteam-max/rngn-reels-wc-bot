from __future__ import annotations

import csv
import io
from typing import Any

import requests
from psycopg.types.json import Jsonb

from bot import db, jobs, multiplatform_metrics


JOB_KIND = "content_core_sync_video"
PLATFORMS = ("instagram", "youtube", "tiktok", "vk")
PLATFORM_URL_COLUMNS = {
    "instagram": "Instagram URL",
    "youtube": "YouTube URL",
    "tiktok": "TikTok URL",
    "vk": "VK URL",
}

_PRODUCER_INSTALLED = False
_WORKER_INSTALLED = False


class ContentCoreConflict(RuntimeError):
    pass


def _bridge_parts() -> tuple[str, str]:
    bridge = multiplatform_metrics.CONTENT_CORE_BRIDGE_URL
    prefix, marker, rest = bridge.partition("/mirror/")
    if not marker or "/" not in rest:
        raise RuntimeError("Content Core bridge URL has unexpected format")
    token = rest.split("/", 1)[0]
    return prefix.rstrip("/"), token


def _urls() -> tuple[str, str, str]:
    prefix, token = _bridge_parts()
    return (
        f"{prefix}/mirror/{token}/videos-v2.tsv",
        f"{prefix}/editor/{token}/attach-publication.tsv",
        multiplatform_metrics.CONTENT_CORE_BRIDGE_URL,
    )


def _safe_error(value: Any) -> str:
    text = str(value or "").replace("\n", " ")
    try:
        _, token = _bridge_parts()
        text = text.replace(token, "[core-token]")
    except Exception:
        pass
    return text[:500]


def ensure_schema() -> None:
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS content_core_video_links (
            video_id bigint PRIMARY KEY REFERENCES videos(id) ON DELETE CASCADE,
            content_core_video_id text NULL,
            resolve_status text NOT NULL DEFAULT 'unresolved',
            matched_by_platform text NULL,
            last_attempt_at timestamptz NULL,
            last_resolved_at timestamptz NULL,
            last_error text NULL,
            details jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    db.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_content_core_video_links_core_id
        ON content_core_video_links(content_core_video_id)
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS content_core_publication_links (
            video_id bigint NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
            content_core_publication_id text NOT NULL,
            platform text NOT NULL,
            platform_video_id text NULL,
            url text NULL,
            first_seen_at timestamptz NOT NULL DEFAULT now(),
            last_seen_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (video_id, content_core_publication_id)
        )
        """
    )
    db.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_content_core_publication_links_video_platform
        ON content_core_publication_links(video_id, platform)
        """
    )


def _load_video(video_id: int) -> dict[str, Any] | None:
    from bot import handlers as h

    return db.fetch_one(
        h.VIDEO_SELECT + " WHERE v.id = %s",
        (int(video_id),),
    )


def _fetch_tsv(url: str, *, timeout: int = 45) -> list[dict[str, str]]:
    response = requests.get(url, timeout=timeout)
    response.raise_for_status()
    return list(csv.DictReader(io.StringIO(response.text), delimiter="\t"))


def _core_platform_id(row: dict[str, str], platform: str) -> str:
    url = str(row.get(PLATFORM_URL_COLUMNS[platform]) or "").strip()
    if not url:
        return ""
    probe = {
        f"{platform}_url": url,
        f"{platform}_id": "",
    }
    return multiplatform_metrics._platform_id(probe, platform)


def _raw_target(row: dict[str, str]) -> str:
    raw_ids = [
        value.strip()
        for value in str(row.get("raw_video_ids") or "").split(",")
        if value.strip()
    ]
    if raw_ids:
        return raw_ids[0]
    video_id = str(row.get("video_id") or "").strip()
    return video_id if video_id and not video_id.startswith("group-") else ""


def _resolve_target(
    video: dict[str, Any],
    rows: list[dict[str, str]],
) -> tuple[str, str, dict[str, list[str]]]:
    candidates: dict[str, list[str]] = {}
    for platform in PLATFORMS:
        bot_id = multiplatform_metrics._platform_id(video, platform)
        if not bot_id:
            continue
        found: list[str] = []
        for row in rows:
            if _core_platform_id(row, platform) != bot_id:
                continue
            target = _raw_target(row)
            if target:
                found.append(target)
        if found:
            candidates[platform] = list(dict.fromkeys(found))

    for platform in ("instagram", "tiktok", "youtube", "vk"):
        options = candidates.get(platform) or []
        if len(options) == 1:
            return options[0], platform, candidates
        if len(options) > 1:
            raise ContentCoreConflict(
                f"Content Core has multiple {platform} candidates for bot video #{video['id']}: {options}"
            )
    return "", "", candidates


def _submitted_urls(video: dict[str, Any]) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    for platform in PLATFORMS:
        url = str(video.get(f"{platform}_url") or "").strip()
        if url:
            result.append((platform, url))
    return result


def _set_link_state(
    video_id: int,
    *,
    status: str,
    core_video_id: str | None = None,
    matched_by_platform: str | None = None,
    error: str | None = None,
    details: dict[str, Any] | None = None,
    resolved: bool = False,
) -> None:
    ensure_schema()
    db.execute(
        """
        INSERT INTO content_core_video_links (
            video_id, content_core_video_id, resolve_status,
            matched_by_platform, last_attempt_at, last_resolved_at,
            last_error, details, created_at, updated_at
        )
        VALUES (
            %s, %s, %s, %s, now(),
            CASE WHEN %s THEN now() ELSE NULL END,
            %s, %s, now(), now()
        )
        ON CONFLICT (video_id)
        DO UPDATE SET
            content_core_video_id = COALESCE(
                EXCLUDED.content_core_video_id,
                content_core_video_links.content_core_video_id
            ),
            resolve_status = EXCLUDED.resolve_status,
            matched_by_platform = COALESCE(
                EXCLUDED.matched_by_platform,
                content_core_video_links.matched_by_platform
            ),
            last_attempt_at = now(),
            last_resolved_at = CASE
                WHEN %s THEN now()
                ELSE content_core_video_links.last_resolved_at
            END,
            last_error = EXCLUDED.last_error,
            details = EXCLUDED.details,
            updated_at = now()
        """,
        (
            int(video_id),
            core_video_id,
            status,
            matched_by_platform,
            bool(resolved),
            _safe_error(error) if error else None,
            Jsonb(details or {}),
            bool(resolved),
        ),
    )


def _attach_url(target: str, platform: str, url: str) -> tuple[bool, int, str]:
    del platform
    _, attach_url, _ = _urls()
    response = requests.get(
        attach_url,
        params={"video": target, "url": url, "wave": "0"},
        timeout=50,
    )
    text = response.text.strip()
    return response.ok and text.startswith("OK:"), int(response.status_code), text[:300]


def _refresh_publication_links(video: dict[str, Any]) -> int:
    ensure_schema()
    _, _, bridge_url = _urls()
    rows = _fetch_tsv(bridge_url)
    matched = 0
    for platform in PLATFORMS:
        platform_id = multiplatform_metrics._platform_id(video, platform)
        if not platform_id:
            continue
        for row in rows:
            if str(row.get("platform") or "") != platform:
                continue
            match_id = str(row.get("match_id") or "").strip()
            if platform == "vk":
                match_id = multiplatform_metrics._normalize_vk_id(match_id)
            if match_id != platform_id:
                continue
            publication_id = str(row.get("publication_id") or "").strip()
            if not publication_id:
                continue
            db.execute(
                """
                INSERT INTO content_core_publication_links (
                    video_id, content_core_publication_id, platform,
                    platform_video_id, url, first_seen_at, last_seen_at
                )
                VALUES (%s, %s, %s, %s, %s, now(), now())
                ON CONFLICT (video_id, content_core_publication_id)
                DO UPDATE SET
                    platform = EXCLUDED.platform,
                    platform_video_id = EXCLUDED.platform_video_id,
                    url = EXCLUDED.url,
                    last_seen_at = now()
                """,
                (
                    int(video["id"]),
                    publication_id,
                    platform,
                    platform_id,
                    str(row.get("url") or "").strip() or None,
                ),
            )
            matched += 1
    return matched


def sync_approved_video(video_id: int) -> dict[str, Any]:
    ensure_schema()
    video = _load_video(video_id)
    if not video:
        raise ContentCoreConflict(f"bot video #{video_id} not found")
    if str(video.get("status") or "") != "approved":
        _set_link_state(
            video_id,
            status="skipped",
            details={"reason": f"status={video.get('status')}"},
        )
        return {"status": "skipped", "video_id": video_id}

    videos_url, _, _ = _urls()
    rows = _fetch_tsv(videos_url)
    try:
        target, matched_by, candidates = _resolve_target(video, rows)
    except ContentCoreConflict as exc:
        _set_link_state(
            video_id,
            status="conflict",
            error=str(exc),
            details={"stage": "resolve"},
        )
        raise

    if not target:
        message = "approved video is not present in Content Core yet"
        _set_link_state(
            video_id,
            status="unmatched",
            error=message,
            details={"candidates": candidates},
        )
        raise RuntimeError(message)

    submitted = _submitted_urls(video)
    results: list[dict[str, Any]] = []
    pending = False
    conflicts: list[str] = []
    for platform, url in submitted:
        try:
            ok, status_code, text = _attach_url(target, platform, url)
        except Exception as exc:
            pending = True
            results.append(
                {
                    "platform": platform,
                    "ok": False,
                    "status": None,
                    "error": _safe_error(exc),
                }
            )
            continue
        results.append(
            {
                "platform": platform,
                "ok": ok,
                "status": status_code,
                "response": text,
            }
        )
        if ok:
            continue
        if status_code == 400:
            conflicts.append(f"{platform}: {text}")
        else:
            pending = True

    if conflicts:
        error = " | ".join(conflicts)
        _set_link_state(
            video_id,
            status="conflict",
            core_video_id=target,
            matched_by_platform=matched_by,
            error=error,
            details={"attachments": results, "candidates": candidates},
        )
        raise ContentCoreConflict(error)

    publication_links = 0
    try:
        publication_links = _refresh_publication_links(video)
    except Exception as exc:
        results.append({"publication_link_refresh_error": _safe_error(exc)})

    status = "partial" if pending else "resolved"
    _set_link_state(
        video_id,
        status=status,
        core_video_id=target,
        matched_by_platform=matched_by,
        error=(
            "one or more publications are pending in Content Core"
            if pending
            else None
        ),
        details={
            "attachments": results,
            "candidates": candidates,
            "publication_links": publication_links,
        },
        resolved=not pending,
    )

    with db.transaction() as conn:
        db.log_event(
            conn,
            entity_type="content_core_link",
            entity_id=int(video_id),
            action=f"content_core_{status}",
            actor_username="system",
            after_data={
                "content_core_video_id": target,
                "matched_by_platform": matched_by,
                "publication_links": publication_links,
                "attachments": results,
            },
        )

    if pending:
        raise RuntimeError("one or more Content Core publications are still pending")

    return {
        "status": status,
        "video_id": int(video_id),
        "content_core_video_id": target,
        "matched_by_platform": matched_by,
        "publication_links": publication_links,
    }


def _status_in_connection(conn, video_id: int) -> str:
    if conn is None:
        row = db.fetch_one(
            "SELECT status FROM videos WHERE id = %s",
            (int(video_id),),
        )
        return str((row or {}).get("status") or "")
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM videos WHERE id = %s", (int(video_id),))
        row = cur.fetchone()
    return str((row or {}).get("status") or "")


def install_submission_hooks() -> None:
    global _PRODUCER_INSTALLED
    if _PRODUCER_INSTALLED:
        return

    jobs.ALLOWED_JOB_KINDS.add(JOB_KIND)
    original_enqueue_sheet_sync = jobs.enqueue_sheet_sync

    def enqueue_sheet_sync(
        video_id: int,
        *,
        version: str | None = None,
        conn=None,
    ) -> int | None:
        sheet_job_id = original_enqueue_sheet_sync(
            video_id,
            version=version,
            conn=conn,
        )
        if _status_in_connection(conn, int(video_id)) == "approved":
            jobs.enqueue_job(
                JOB_KIND,
                {"video_id": int(video_id)},
                dedupe_key=f"content-core:video:{int(video_id)}",
                priority=65,
                max_attempts=8,
                conn=conn,
            )
        return sheet_job_id

    jobs.enqueue_sheet_sync = enqueue_sheet_sync
    _PRODUCER_INSTALLED = True


def install_worker() -> None:
    global _WORKER_INSTALLED
    if _WORKER_INSTALLED:
        return

    from bot import handlers as h
    from bot import job_worker

    jobs.ALLOWED_JOB_KINDS.add(JOB_KIND)

    def handle_content_core_sync(
        payload: dict[str, Any],
        context,
    ) -> None:
        del context
        video_id = int(payload.get("video_id") or 0)
        if not video_id:
            raise job_worker.PermanentJobError(
                "content_core_sync_video requires video_id"
            )
        try:
            result = sync_approved_video(video_id)
        except ContentCoreConflict as exc:
            raise job_worker.PermanentJobError(_safe_error(exc)) from exc
        h.record_system_log(
            "content_core_sync_done",
            "video",
            video_id,
            result,
        )

    job_worker.JOB_HANDLERS[JOB_KIND] = handle_content_core_sync
    _WORKER_INSTALLED = True
