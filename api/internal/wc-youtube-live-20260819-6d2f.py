from __future__ import annotations

import json
from collections import defaultdict
from http.server import BaseHTTPRequestHandler
from typing import Any

from bot import db
from bot.config import get_settings
from bot.github_oidc import GitHubOIDCError, validate_github_oidc_token
from bot.links import extract_youtube_id
from bot.youtube_metrics import YouTubeAPIError, fetch_youtube_statistics

WORLD_CUP_CODE = "world_cup_2026"


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _authorized(headers) -> bool:
    authorization = headers.get("Authorization") or ""
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token or token.count(".") != 2:
        return False
    try:
        validate_github_oidc_token(token)
    except GitHubOIDCError:
        return False
    return True


def _world_cup_youtube_rows() -> list[dict[str, Any]]:
    return db.fetch_all(
        """
        SELECT
            v.id AS bot_video_id,
            COALESCE(NULLIF(v.author_name, ''), author_p.name, 'Без автора') AS author,
            v.youtube_id,
            v.youtube_url
        FROM videos v
        LEFT JOIN people author_p ON author_p.id = v.author_id
        WHERE v.status = 'approved'
          AND v.project_code = %s
          AND (
              (v.youtube_id IS NOT NULL AND btrim(v.youtube_id) <> '')
              OR (v.youtube_url IS NOT NULL AND btrim(v.youtube_url) <> '')
          )
        ORDER BY v.id ASC
        """,
        (WORLD_CUP_CODE,),
    )


def _resolve_id(row: dict[str, Any]) -> str | None:
    direct = str(row.get("youtube_id") or "").strip()
    if direct:
        return direct
    url = str(row.get("youtube_url") or "").strip()
    if not url:
        return None
    try:
        return extract_youtube_id(url)
    except Exception:
        return None


def build_report() -> dict[str, Any]:
    settings = get_settings()
    if not settings.youtube_api_key:
        raise RuntimeError("YOUTUBE_API_KEY not configured")

    rows = _world_cup_youtube_rows()
    resolved: list[tuple[dict[str, Any], str]] = []
    unresolved: list[int] = []
    for row in rows:
        youtube_id = _resolve_id(row)
        if youtube_id:
            resolved.append((row, youtube_id))
        else:
            unresolved.append(int(row["bot_video_id"]))

    ids = list(dict.fromkeys(youtube_id for _, youtube_id in resolved))
    stats_by_id = {}
    api_missing: list[str] = []
    api_errors: list[str] = []
    for offset in range(0, len(ids), 50):
        batch = ids[offset : offset + 50]
        try:
            stats, missing = fetch_youtube_statistics(batch, settings.youtube_api_key)
        except YouTubeAPIError as exc:
            api_errors.append(f"{exc.status_code or ''}:{exc.description}"[:300])
            continue
        stats_by_id.update(stats)
        api_missing.extend(missing)

    videos: list[dict[str, Any]] = []
    authors: dict[str, dict[str, int]] = defaultdict(lambda: {"videos": 0, "videos_with_stats": 0, "views": 0})
    total_views = 0
    for row, youtube_id in resolved:
        author = str(row.get("author") or "Без автора")
        authors[author]["videos"] += 1
        stat = stats_by_id.get(youtube_id)
        item = {
            "bot_video_id": int(row["bot_video_id"]),
            "author": author,
            "youtube_id": youtube_id,
            "views": None,
            "likes": None,
            "comments": None,
        }
        if stat is not None:
            views = int(stat.views or 0)
            item["views"] = stat.views
            item["likes"] = stat.likes
            item["comments"] = stat.comments
            authors[author]["videos_with_stats"] += 1
            authors[author]["views"] += views
            total_views += views
        videos.append(item)

    author_rows = [
        {"author": author, **values}
        for author, values in authors.items()
    ]
    author_rows.sort(key=lambda item: (-item["views"], item["author"].casefold()))

    return {
        "ok": True,
        "project_code": WORLD_CUP_CODE,
        "db_rows_with_youtube": len(rows),
        "resolved_youtube_ids": len(resolved),
        "unique_youtube_ids": len(ids),
        "videos_with_live_stats": sum(1 for video in videos if video["views"] is not None),
        "total_views": total_views,
        "unresolved_bot_video_ids": unresolved,
        "api_missing_ids": sorted(set(api_missing)),
        "api_errors": api_errors,
        "authors": author_rows,
        "videos": videos,
    }


class handler(BaseHTTPRequestHandler):
    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = _json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_POST(self) -> None:
        if not _authorized(self.headers):
            self._send_json(401, {"ok": False, "error": "unauthorized"})
            return
        try:
            report = build_report()
        except Exception as exc:
            self._send_json(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]})
            return
        self._send_json(200, report)

    def do_GET(self) -> None:
        self._send_json(405, {"ok": False, "error": "method not allowed"})

    def do_HEAD(self) -> None:
        self._send_json(405, {"ok": False, "error": "method not allowed"})
