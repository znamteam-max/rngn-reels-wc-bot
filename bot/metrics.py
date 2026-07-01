from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from typing import Any

from psycopg.types.json import Jsonb

from bot import db, sheets, youtube_metrics
from bot.config import get_settings
from bot.links import normalize_youtube


PLATFORM_YOUTUBE = "youtube"

APPROVED_YOUTUBE_SELECT = """
SELECT
    v.*,
    COALESCE(v.author_name, author_p.name) AS author_name,
    COALESCE(v.author_username, author_p.username) AS author_username,
    author_p.tg_id AS author_tg_id,
    COALESCE(v.montage_name, montage_p.name) AS montage_name,
    COALESCE(v.montage_username, montage_p.username) AS montage_username,
    montage_p.tg_id AS montage_tg_id,
    COALESCE(v.voice_name, voice_p.name) AS voice_name,
    COALESCE(v.voice_username, voice_p.username) AS voice_username,
    voice_p.tg_id AS voice_tg_id
FROM videos v
LEFT JOIN people author_p ON author_p.id = v.author_id
LEFT JOIN people montage_p ON montage_p.id = v.montage_id
LEFT JOIN people voice_p ON voice_p.id = v.voice_id
WHERE v.status = 'approved'
  AND v.youtube_url IS NOT NULL
  AND btrim(v.youtube_url) <> ''
ORDER BY v.id ASC
"""


@dataclass
class YouTubeSyncResult:
    missing_key: bool = False
    no_videos: bool = False
    total_videos: int = 0
    success_count: int = 0
    error_count: int = 0
    total_views: int = 0
    total_likes: int = 0
    total_comments: int = 0
    top_video_id: int | None = None
    top_views: int = 0
    sheet_appended: int = 0
    sheet_status: str = "skipped"
    sheet_error: str | None = None
    snapshots: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.missing_key and not self.no_videos and self.error_count == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "missing_key": self.missing_key,
            "no_videos": self.no_videos,
            "total_videos": self.total_videos,
            "success_count": self.success_count,
            "error_count": self.error_count,
            "total_views": self.total_views,
            "total_likes": self.total_likes,
            "total_comments": self.total_comments,
            "top_video_id": self.top_video_id,
            "top_views": self.top_views,
            "sheet_appended": self.sheet_appended,
            "sheet_status": self.sheet_status,
            "sheet_error": self.sheet_error,
        }


def _safe_error(exc: Exception) -> str:
    token = get_settings().youtube_api_key or ""
    text = str(exc)
    if token:
        text = text.replace(token, "[youtube-api-key]")
    return text[:500]


def _int_value(value: Any) -> int:
    if value is None or value == "":
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def format_number(value: Any) -> str:
    return f"{_int_value(value):,}".replace(",", " ")


def metric_day_bounds(day: date) -> tuple[datetime, datetime]:
    tz = get_settings().tz
    start = datetime.combine(day, time.min, tzinfo=tz)
    end = datetime.combine(day, time.max, tzinfo=tz)
    return start, end


def today_bounds() -> tuple[datetime, datetime]:
    return metric_day_bounds(datetime.now(get_settings().tz).date())


def chunked(items: list[str], size: int) -> list[list[str]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


def summarize_metric_rows(rows: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "count": len(rows),
        "views": sum(_int_value(row.get("views")) for row in rows),
        "likes": sum(_int_value(row.get("likes")) for row in rows),
        "comments": sum(_int_value(row.get("comments")) for row in rows),
    }


def top_metric_rows(rows: list[dict[str, Any]], limit: int = 5) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda row: _int_value(row.get("views")), reverse=True)[:limit]


def latest_snapshot_per_video(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    latest: dict[int, dict[str, Any]] = {}
    for row in rows:
        video_id = int(row["video_id"])
        current = latest.get(video_id)
        if not current or row.get("captured_at") > current.get("captured_at"):
            latest[video_id] = row
    return list(latest.values())


def approved_youtube_videos() -> list[dict[str, Any]]:
    return db.fetch_all(APPROVED_YOUTUBE_SELECT)


def approved_youtube_video_count() -> int:
    row = db.fetch_one(
        """
        SELECT count(*) AS count
        FROM videos
        WHERE status = 'approved'
          AND youtube_url IS NOT NULL
          AND btrim(youtube_url) <> ''
        """
    )
    return int((row or {}).get("count") or 0)


def ensure_video_youtube_id(video: dict[str, Any]) -> str | None:
    youtube_id = (video.get("youtube_id") or "").strip()
    if youtube_id:
        return youtube_id
    try:
        youtube_id = normalize_youtube(str(video.get("youtube_url") or "")).external_id or ""
    except Exception:
        youtube_id = ""
    if youtube_id:
        db.execute(
            "UPDATE videos SET youtube_id = %s, updated_at = now() WHERE id = %s",
            (youtube_id, int(video["id"])),
        )
        video["youtube_id"] = youtube_id
    return youtube_id or None


def _snapshot_day_bounds(captured_at: datetime) -> tuple[datetime, datetime]:
    local_day = captured_at.astimezone(get_settings().tz).date()
    return metric_day_bounds(local_day)


def upsert_metric_snapshot(
    video: dict[str, Any],
    *,
    captured_at: datetime,
    platform_video_id: str | None,
    views: int | None,
    likes: int | None,
    comments: int | None,
    source_status: str,
    error_message: str | None = None,
    raw_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    day_start, day_end = _snapshot_day_bounds(captured_at)
    with db.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id
                FROM video_metrics_snapshots
                WHERE video_id = %s
                  AND platform = %s
                  AND captured_at >= %s
                  AND captured_at <= %s
                ORDER BY captured_at DESC
                LIMIT 1
                """,
                (int(video["id"]), PLATFORM_YOUTUBE, day_start, day_end),
            )
            existing = cur.fetchone()
            values = (
                platform_video_id,
                video.get("youtube_url"),
                captured_at,
                views,
                likes,
                comments,
                None,
                source_status,
                error_message,
                Jsonb(raw_data) if raw_data is not None else None,
            )
            if existing:
                cur.execute(
                    """
                    UPDATE video_metrics_snapshots
                    SET platform_video_id = %s,
                        platform_url = %s,
                        captured_at = %s,
                        views = %s,
                        likes = %s,
                        comments = %s,
                        shares = %s,
                        source_status = %s,
                        error_message = %s,
                        raw_data = %s
                    WHERE id = %s
                    RETURNING *
                    """,
                    (*values, int(existing["id"])),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO video_metrics_snapshots (
                        video_id, platform, platform_video_id, platform_url,
                        captured_at, views, likes, comments, shares,
                        source_status, error_message, raw_data
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING *
                    """,
                    (int(video["id"]), PLATFORM_YOUTUBE, *values),
                )
            snapshot = cur.fetchone()
            if source_status == "ok":
                cur.execute(
                    """
                    UPDATE videos
                    SET youtube_id = COALESCE(%s, youtube_id),
                        youtube_views = %s,
                        youtube_likes = %s,
                        youtube_comments = %s,
                        youtube_last_sync_at = %s,
                        updated_at = now()
                    WHERE id = %s
                    """,
                    (platform_video_id, views, likes, comments, captured_at, int(video["id"])),
                )
        return {**video, **snapshot}


def log_metrics_event(
    action: str,
    after_data: dict[str, Any],
    actor_tg_id: int | None = None,
    actor_username: str | None = None,
) -> None:
    try:
        with db.transaction() as conn:
            db.log_event(
                conn,
                entity_type="metrics",
                entity_id=None,
                action=action,
                actor_tg_id=actor_tg_id,
                actor_username=actor_username,
                after_data=after_data,
            )
    except Exception:
        pass


def sync_youtube_metrics(
    *,
    actor_tg_id: int | None = None,
    actor_username: str | None = None,
) -> YouTubeSyncResult:
    settings = get_settings()
    if not settings.youtube_api_key:
        return YouTubeSyncResult(missing_key=True)

    videos = approved_youtube_videos()
    if not videos:
        return YouTubeSyncResult(no_videos=True)

    captured_at = datetime.now(timezone.utc)
    result = YouTubeSyncResult(total_videos=len(videos))
    videos_by_youtube_id: dict[str, list[dict[str, Any]]] = {}

    for video in videos:
        youtube_id = ensure_video_youtube_id(video)
        if not youtube_id:
            result.error_count += 1
            result.snapshots.append(
                upsert_metric_snapshot(
                    video,
                    captured_at=captured_at,
                    platform_video_id=None,
                    views=None,
                    likes=None,
                    comments=None,
                    source_status="error",
                    error_message="Cannot extract YouTube video ID",
                )
            )
            continue
        videos_by_youtube_id.setdefault(youtube_id, []).append(video)

    for ids in chunked(list(videos_by_youtube_id.keys()), 50):
        try:
            stats_by_id, missing_ids = youtube_metrics.fetch_youtube_statistics(ids, settings.youtube_api_key)
        except Exception as exc:
            error = _safe_error(exc)
            for youtube_id in ids:
                for video in videos_by_youtube_id[youtube_id]:
                    result.error_count += 1
                    result.snapshots.append(
                        upsert_metric_snapshot(
                            video,
                            captured_at=captured_at,
                            platform_video_id=youtube_id,
                            views=None,
                            likes=None,
                            comments=None,
                            source_status="error",
                            error_message=error,
                        )
                    )
            continue

        for youtube_id, stats in stats_by_id.items():
            for video in videos_by_youtube_id[youtube_id]:
                snapshot = upsert_metric_snapshot(
                    video,
                    captured_at=captured_at,
                    platform_video_id=youtube_id,
                    views=stats.views,
                    likes=stats.likes,
                    comments=stats.comments,
                    source_status="ok",
                    raw_data=stats.raw_data,
                )
                result.snapshots.append(snapshot)
                result.success_count += 1

        for youtube_id in missing_ids:
            for video in videos_by_youtube_id[youtube_id]:
                result.error_count += 1
                result.snapshots.append(
                    upsert_metric_snapshot(
                        video,
                        captured_at=captured_at,
                        platform_video_id=youtube_id,
                        views=None,
                        likes=None,
                        comments=None,
                        source_status="not_found",
                        error_message="YouTube API item not found",
                    )
                )

    ok_snapshots = [snapshot for snapshot in result.snapshots if snapshot.get("source_status") == "ok"]
    totals = summarize_metric_rows(ok_snapshots)
    result.total_views = totals["views"]
    result.total_likes = totals["likes"]
    result.total_comments = totals["comments"]
    top = top_metric_rows(ok_snapshots, 1)
    if top:
        result.top_video_id = int(top[0]["video_id"])
        result.top_views = _int_value(top[0].get("views"))

    try:
        result.sheet_appended = sheets.append_metric_snapshots(ok_snapshots)
        result.sheet_status = "ok"
    except Exception as exc:
        result.sheet_status = "failed"
        result.sheet_error = _safe_error(exc)

    log_metrics_event(
        "youtube_metrics_sync",
        result.to_dict(),
        actor_tg_id,
        actor_username,
    )
    return result


def format_sync_result(result: YouTubeSyncResult) -> str:
    if result.missing_key:
        return "YOUTUBE_API_KEY не настроен. Добавь ключ в Vercel env."
    if result.no_videos:
        return "Пока нет approved-видео с YouTube-ссылками."

    lines = [
        "YouTube-метрики обновлены",
        "",
        f"Видео с YouTube: {result.total_videos}",
        f"Успешно: {result.success_count}",
        f"Ошибок: {result.error_count}",
        "",
        "Всего:",
        f"Просмотры: {format_number(result.total_views)}",
        f"Лайки: {format_number(result.total_likes)}",
        f"Комментарии: {format_number(result.total_comments)}",
    ]
    if result.top_video_id is not None:
        lines.extend(
            [
                "",
                "Лучший ролик:",
                f"ID {result.top_video_id} — {format_number(result.top_views)} просмотров",
            ]
        )
    if result.sheet_status == "failed":
        lines.extend(["", f"MetricsRaw: не обновлён ({result.sheet_error})"])
    elif result.sheet_status == "ok":
        lines.extend(["", f"MetricsRaw: добавлено строк {result.sheet_appended}"])
    return "\n".join(lines)


def latest_youtube_snapshots() -> list[dict[str, Any]]:
    return db.fetch_all(
        """
        SELECT DISTINCT ON (video_id) *
        FROM video_metrics_snapshots
        WHERE platform = %s
          AND source_status = 'ok'
        ORDER BY video_id, captured_at DESC, id DESC
        """,
        (PLATFORM_YOUTUBE,),
    )


def latest_youtube_snapshots_today() -> list[dict[str, Any]]:
    start, end = today_bounds()
    return db.fetch_all(
        """
        SELECT DISTINCT ON (video_id) *
        FROM video_metrics_snapshots
        WHERE platform = %s
          AND source_status = 'ok'
          AND captured_at >= %s
          AND captured_at <= %s
        ORDER BY video_id, captured_at DESC, id DESC
        """,
        (PLATFORM_YOUTUBE, start, end),
    )


def format_youtube_summary(title: str, rows: list[dict[str, Any]], top_limit: int) -> str:
    if not rows:
        return f"{title}\n\nПока нет YouTube-метрик."
    totals = summarize_metric_rows(rows)
    lines = [
        title,
        "",
        f"Видео{' с метриками' if title.endswith('всего') else ''}: {totals['count']}",
        f"Просмотры: {format_number(totals['views'])}",
        f"Лайки: {format_number(totals['likes'])}",
        f"Комментарии: {format_number(totals['comments'])}",
        "",
        "Топ по просмотрам:" if top_limit == 3 else "Топ-5 по просмотрам:",
    ]
    for index, row in enumerate(top_metric_rows(rows, top_limit), start=1):
        lines.append(f"{index}. ID {row['video_id']} — {format_number(row.get('views'))}")
    return "\n".join(lines)


def format_youtube_today() -> str:
    return format_youtube_summary("YouTube сегодня", latest_youtube_snapshots_today(), 3)


def format_youtube_all() -> str:
    return format_youtube_summary("YouTube всего", latest_youtube_snapshots(), 5)


def get_video_metric_rows(video_id: int) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    video = db.fetch_one(
        """
        SELECT *
        FROM videos
        WHERE id = %s
        """,
        (video_id,),
    )
    rows = db.fetch_all(
        """
        SELECT *
        FROM video_metrics_snapshots
        WHERE video_id = %s
          AND platform = %s
          AND source_status = 'ok'
        ORDER BY captured_at DESC, id DESC
        LIMIT 2
        """,
        (video_id, PLATFORM_YOUTUBE),
    )
    return video, rows


def _format_datetime(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone(get_settings().tz).strftime("%Y-%m-%d %H:%M")
    return str(value) if value else "не указано"


def _delta_line(label: str, latest: dict[str, Any], previous: dict[str, Any], key: str) -> str:
    delta = _int_value(latest.get(key)) - _int_value(previous.get(key))
    sign = "+" if delta >= 0 else ""
    return f"{label}: {sign}{format_number(delta)}"


def format_video_metrics(video_id: int) -> str:
    video, rows = get_video_metric_rows(video_id)
    if not video:
        return f"Видео ID {video_id} не найдено."
    if not rows:
        return f"Метрики видео ID {video_id}\n\nПока нет YouTube-метрик."

    latest = rows[0]
    lines = [
        f"Метрики видео ID {video_id}",
        "",
        f"Instagram: {video.get('instagram_url') or 'нет'}",
        f"YouTube: {video.get('youtube_url') or 'нет'}",
        "",
        "YouTube latest:",
        f"Просмотры: {format_number(latest.get('views'))}",
        f"Лайки: {format_number(latest.get('likes'))}",
        f"Комментарии: {format_number(latest.get('comments'))}",
        f"Обновлено: {_format_datetime(latest.get('captured_at'))}",
        "",
    ]
    if len(rows) < 2:
        lines.append("Прирост за сутки: пока нет второго снимка.")
    else:
        previous = rows[1]
        lines.extend(
            [
                "Прирост за сутки:",
                _delta_line("Просмотры", latest, previous, "views"),
                _delta_line("Лайки", latest, previous, "likes"),
                _delta_line("Комментарии", latest, previous, "comments"),
            ]
        )
    return "\n".join(lines)
