from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from psycopg.types.json import Jsonb

from bot import db
from bot.config import get_settings
from bot.telegram import TelegramClient


ROLE_LABELS = {
    "author": "Автор",
    "montage": "Монтаж",
    "voice": "Озвучка",
}


def previous_report_date(now: datetime | None = None) -> date:
    current = now or datetime.now(get_settings().tz)
    return current.astimezone(get_settings().tz).date() - timedelta(days=1)


def report_day_bounds(report_date: date) -> tuple[datetime, datetime]:
    tz = get_settings().tz
    start = datetime.combine(report_date, time.min, tzinfo=tz)
    end = start + timedelta(days=1)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def _pending_age_seconds(oldest: Any, now: datetime | None = None) -> int | None:
    if not isinstance(oldest, datetime):
        return None
    value = oldest if oldest.tzinfo else oldest.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    return max(0, int((current.astimezone(timezone.utc) - value.astimezone(timezone.utc)).total_seconds()))


def _human_age(seconds: int | None) -> str:
    if seconds is None:
        return "—"
    days, remainder = divmod(max(0, seconds), 86400)
    hours = remainder // 3600
    return f"{days} дн. {hours} ч." if days else f"{hours} ч."


def build_daily_report_snapshot(conn, report_date: date, *, now: datetime | None = None) -> dict[str, Any]:
    start, end = report_day_bounds(report_date)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                count(*) FILTER (
                    WHERE status = 'approved' AND checked_at >= %s AND checked_at < %s
                ) AS approved_count,
                count(*) FILTER (WHERE created_at >= %s AND created_at < %s) AS created_count,
                count(*) FILTER (WHERE status = 'pending') AS pending_count,
                min(created_at) FILTER (WHERE status = 'pending') AS oldest_pending
            FROM videos
            """,
            (start, end, start, end),
        )
        totals = cur.fetchone()
        cur.execute(
            """
            SELECT
                COALESCE(project_code, 'unassigned') AS project_code,
                COALESCE(NULLIF(project_name, ''), 'Без проекта') AS project_name,
                count(*) AS count
            FROM videos
            WHERE status = 'approved'
              AND checked_at >= %s
              AND checked_at < %s
            GROUP BY
                COALESCE(project_code, 'unassigned'),
                COALESCE(NULLIF(project_name, ''), 'Без проекта')
            ORDER BY count(*) DESC, project_name
            """,
            (start, end),
        )
        project_rows = list(cur.fetchall())
        cur.execute(
            """
            SELECT role, person_name, person_username, count(*) AS count
            FROM (
                SELECT 'author' AS role, author_name AS person_name, author_username AS person_username
                FROM videos
                WHERE status = 'approved' AND checked_at >= %s AND checked_at < %s
                UNION ALL
                SELECT 'montage', montage_name, montage_username
                FROM videos
                WHERE status = 'approved' AND checked_at >= %s AND checked_at < %s
                UNION ALL
                SELECT 'voice', voice_name, voice_username
                FROM videos
                WHERE status = 'approved' AND checked_at >= %s AND checked_at < %s
            ) roles
            WHERE COALESCE(person_name, '') <> ''
            GROUP BY role, person_name, person_username
            ORDER BY role, count(*) DESC, person_name
            """,
            (start, end, start, end, start, end),
        )
        role_rows = list(cur.fetchall())

    top_roles: dict[str, dict[str, Any]] = {}
    for row in role_rows:
        top_roles.setdefault(str(row["role"]), row)
    return {
        "report_date": report_date,
        "approved_count": int(totals["approved_count"] or 0),
        "created_count": int(totals["created_count"] or 0),
        "pending_count": int(totals["pending_count"] or 0),
        "oldest_pending_age_seconds": _pending_age_seconds(totals.get("oldest_pending"), now),
        "projects": [
            {
                "project_code": row["project_code"],
                "project_name": row["project_name"],
                "count": int(row["count"]),
            }
            for row in project_rows
        ],
        "top_roles": {
            role: {
                "name": row["person_name"],
                "username": row.get("person_username"),
                "count": int(row["count"]),
            }
            for role, row in top_roles.items()
        },
    }


def _project_emoji(code: str) -> str:
    return {
        "vzyal_myach": "🏀",
        "bolshe": "🎾",
        "ves_sport": "🌍",
        "padel_channel": "🎾",
        "home_of_hockey": "🏒",
        "double_play": "🏈",
        "sport_core": "👕",
        "music_core": "🎵",
        "other": "➕",
        "unassigned": "❓",
    }.get(code, "📂")


def format_daily_report(snapshot: dict[str, Any]) -> str:
    report_date = snapshot["report_date"]
    lines = [
        f"📅 ОТЧЁТ ЗА {report_date.strftime('%d.%m.%Y')}",
        "",
        f"Одобрено: {snapshot['approved_count']}",
        f"Добавлено новых заявок: {snapshot['created_count']}",
        f"Осталось неразобранных: {snapshot['pending_count']}",
    ]
    projects = snapshot.get("projects") or []
    if projects:
        lines.extend(["", "По проектам:"])
        lines.extend(
            f"{_project_emoji(row['project_code'])} {row['project_name']} — {row['count']}"
            for row in projects
        )
    top_roles = snapshot.get("top_roles") or {}
    if top_roles:
        lines.extend(["", "Топ по ролям:"])
        for role in ("author", "montage", "voice"):
            row = top_roles.get(role)
            if row:
                lines.append(f"{ROLE_LABELS[role]} — {row['name']}: {row['count']}")
    lines.extend(
        [
            "",
            "Очередь сейчас:",
            f"🔴 {snapshot['pending_count']} ждут проверки"
            if snapshot["pending_count"]
            else "🟢 Очередь разобрана",
        ]
    )
    if snapshot["pending_count"]:
        lines.append(f"Самая старая: {_human_age(snapshot.get('oldest_pending_age_seconds'))}")
    return "\n".join(lines)


def preview_daily_report(report_date: date | None = None) -> tuple[dict[str, Any], str]:
    target = report_date or previous_report_date()
    with db.connect() as conn:
        snapshot = build_daily_report_snapshot(conn, target)
    return snapshot, format_daily_report(snapshot)


def _log_failure(report_date: date, exc: Exception) -> None:
    try:
        with db.transaction() as conn:
            db.log_event(
                conn,
                entity_type="daily_report",
                entity_id=None,
                action="daily_report_failed",
                after_data={"report_date": report_date.isoformat(), "error": f"{type(exc).__name__}: {exc}"[:300]},
            )
    except Exception:
        pass


def send_daily_report(
    report_date: date | None = None,
    *,
    tg: TelegramClient | None = None,
    actor_tg_id: int | None = None,
    actor_username: str | None = None,
) -> dict[str, Any]:
    target = report_date or previous_report_date()
    client = tg or TelegramClient()
    try:
        with db.transaction() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"daily_report:{target.isoformat()}",))
                cur.execute(
                    "SELECT telegram_chat_id, telegram_message_id FROM daily_reports WHERE report_date = %s",
                    (target,),
                )
                existing = cur.fetchone()
            if existing:
                db.log_event(
                    conn,
                    entity_type="daily_report",
                    entity_id=None,
                    action="daily_report_skipped_duplicate",
                    actor_tg_id=actor_tg_id,
                    actor_username=actor_username,
                    after_data={"report_date": target.isoformat()},
                )
                return {
                    "ok": True,
                    "sent": False,
                    "duplicate": True,
                    "report_date": target.isoformat(),
                    "message_id": existing.get("telegram_message_id"),
                }

            snapshot = build_daily_report_snapshot(conn, target)
            text = format_daily_report(snapshot)
            chat_id = int(get_settings().admin_chat_id)
            response = client.send_message(chat_id, text)
            result = response.get("result") if isinstance(response, dict) else None
            message_id = int(result["message_id"]) if isinstance(result, dict) and result.get("message_id") else None
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO daily_reports (
                        report_date, telegram_chat_id, telegram_message_id, payload
                    ) VALUES (%s, %s, %s, %s)
                    """,
                    (target, chat_id, message_id, Jsonb({**snapshot, "report_date": target.isoformat()})),
                )
            db.log_event(
                conn,
                entity_type="daily_report",
                entity_id=None,
                action="daily_report_sent",
                actor_tg_id=actor_tg_id,
                actor_username=actor_username,
                after_data={"report_date": target.isoformat(), "message_id": message_id},
            )
        return {
            "ok": True,
            "sent": True,
            "duplicate": False,
            "report_date": target.isoformat(),
            "message_id": message_id,
        }
    except Exception as exc:
        _log_failure(target, exc)
        raise
