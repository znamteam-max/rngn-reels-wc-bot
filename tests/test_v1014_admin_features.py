from __future__ import annotations

import unittest
from contextlib import contextmanager
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from bot.daily_reports import format_daily_report, report_day_bounds, send_daily_report
from bot.handlers import (
    Actor,
    _format_pending_age,
    _queue_filter_sql,
    _video_matches_queue_filter,
    change_admin_queue_filter,
    find_person_candidates,
    find_videos_exact,
    format_admin_dashboard,
    format_person_profile,
    handle_dashboard_callback,
    load_person_profile,
    queue_status_command,
    resend_pending_command,
    reset_admin_queue_command,
    show_admin,
)


class FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str, object]] = []
        self.edited: list[tuple[int, int, str, object]] = []
        self.answers: list[tuple[str, str | None, bool]] = []

    def send_message(self, chat_id, text, reply_markup=None, disable_web_page_preview=True):
        self.sent.append((int(chat_id), text, reply_markup))
        return {"ok": True, "result": {"message_id": 500}}

    def edit_message_text(self, chat_id, message_id, text, reply_markup=None, disable_web_page_preview=True):
        self.edited.append((int(chat_id), int(message_id), text, reply_markup))
        return {"ok": True}

    def answer_callback_query(self, callback_query_id, text=None, show_alert=False):
        self.answers.append((callback_query_id, text, show_alert))
        return {"ok": True}


class DashboardV1014Tests(unittest.TestCase):
    def test_dashboard_zero_one_many(self) -> None:
        base = {
            "oldest_created_at": datetime.now(timezone.utc),
            "project_counts": [],
            "updated_at": datetime(2026, 8, 3, 17, 25, tzinfo=ZoneInfo("Europe/Moscow")),
        }
        zero = format_admin_dashboard({**base, "pending_count": 0, "active_video_id": None})
        one = format_admin_dashboard({**base, "pending_count": 1, "active_video_id": 36})
        many = format_admin_dashboard({**base, "pending_count": 67, "active_video_id": 36})
        self.assertIn("🟢 Очередь разобрана", zero)
        self.assertIn("🔴 Ждут проверки: 1", one)
        self.assertIn("▶️ Текущая заявка: #36", one)
        self.assertIn("🔴 Ждут проверки: 67", many)

    def test_human_age_days_and_hours(self) -> None:
        now = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
        oldest = datetime(2026, 8, 1, 22, 0, tzinfo=timezone.utc)
        self.assertEqual(_format_pending_age(oldest, now), "1 дн. 14 ч.")

    def test_global_project_unassigned_other_filters(self) -> None:
        self.assertEqual(_queue_filter_sql({"queue_filter_type": "global"}), ("TRUE", ()))
        project_sql, params = _queue_filter_sql(
            {"queue_filter_type": "project", "queue_filter_value": "bolshe"}
        )
        self.assertIn("project_code = %s", project_sql)
        self.assertEqual(params, ("bolshe",))
        self.assertTrue(
            _video_matches_queue_filter(
                {"project_code": "bolshe", "project_name": "Больше"},
                {"queue_filter_type": "project", "queue_filter_value": "bolshe"},
            )
        )
        self.assertTrue(
            _video_matches_queue_filter(
                {"project_code": "other", "project_name": "Шоу"},
                {"queue_filter_type": "other"},
            )
        )
        self.assertTrue(
            _video_matches_queue_filter(
                {"project_code": None, "project_name": None},
                {"queue_filter_type": "unassigned"},
            )
        )
        self.assertFalse(
            _video_matches_queue_filter(
                {"project_code": "bolshe", "project_name": "Больше"},
                {"queue_filter_type": "unassigned"},
            )
        )

    def test_switch_filter_archives_mismatched_pointer_and_pumps_oldest(self) -> None:
        actor = Actor(tg_id=1, chat_id=-1001, username="admin")
        tg = FakeTelegram()
        conn = MagicMock()

        @contextmanager
        def transaction():
            yield conn

        state = {
            "queue_filter_type": "global",
            "queue_filter_value": None,
            "active_video_id": 36,
            "active_chat_id": -1001,
            "active_message_id": 233,
        }
        target = {
            "id": 41,
            "project_code": "bolshe",
            "project_name": "Больше",
            "created_at": datetime(2026, 8, 1, tzinfo=timezone.utc),
        }
        with (
            patch("bot.handlers.get_active_project", return_value={"id": 2, "code": "bolshe", "name": "Больше"}),
            patch("bot.handlers.db.transaction", transaction),
            patch("bot.handlers._queue_state_for_update", return_value=state),
            patch("bot.handlers._oldest_pending_video", return_value=target),
            patch("bot.handlers._clear_queue_state") as clear,
            patch("bot.handlers.db.log_event"),
            patch("bot.handlers._archive_queue_message") as archive,
            patch(
                "bot.handlers.pump_admin_queue",
                return_value={"pending_count": 8, "active_video_id": 41, "active_message_id": 501},
            ) as pump,
            patch("bot.handlers._safe_refresh_admin_dashboard"),
        ):
            result = change_admin_queue_filter(tg, actor, "project", "bolshe")
        clear.assert_called_once_with(conn)
        archive.assert_called_once()
        pump.assert_called_once_with(tg, actor)
        self.assertEqual(result["active_video_id"], 41)

    def test_stale_dashboard_callback_is_rejected(self) -> None:
        actor = Actor(tg_id=1, chat_id=-1001)
        tg = FakeTelegram()
        with (
            patch("bot.handlers.is_admin", return_value=True),
            patch("bot.handlers._dashboard_callback_is_current", return_value=False),
            patch("bot.handlers.pump_admin_queue") as pump,
        ):
            handle_dashboard_callback(tg, actor, "dash:open", 200, "cb1")
        pump.assert_not_called()
        self.assertEqual(tg.answers[-1], ("cb1", "Этот дашборд устарел. Откройте /admin.", True))

    def test_admin_and_resend_pump_only_one_filtered_card(self) -> None:
        actor = Actor(tg_id=1, chat_id=-1001)
        tg = FakeTelegram()
        result = {
            "pending_count": 8,
            "global_pending_count": 67,
            "active_video_id": 41,
            "active_message_id": 500,
            "sent": True,
            "queue_filter_type": "project",
            "queue_filter_value": "bolshe",
        }
        with (
            patch("bot.handlers.require_admin", return_value=True),
            patch("bot.handlers._safe_refresh_admin_dashboard") as refresh,
            patch("bot.handlers.pump_admin_queue", return_value=result) as pump,
            patch("bot.handlers.record_system_log"),
        ):
            show_admin(tg, actor)
            resend_pending_command(tg, actor)
        self.assertEqual(pump.call_count, 2)
        self.assertTrue(all(call.kwargs == {"force_repost": True} for call in pump.call_args_list))
        self.assertEqual(refresh.call_count, 4)

    def test_queue_status_includes_filter_diagnostics(self) -> None:
        actor = Actor(tg_id=1, chat_id=-1001)
        tg = FakeTelegram()
        conn = MagicMock()

        @contextmanager
        def transaction():
            yield conn

        state = {
            "queue_filter_type": "project",
            "queue_filter_value": "bolshe",
            "dashboard_message_id": 234,
        }
        snapshot = {
            "pending_count": 67,
            "active_video_id": 41,
            "oldest_created_at": datetime.now(timezone.utc),
        }
        with (
            patch("bot.handlers.require_admin", return_value=True),
            patch("bot.handlers.db.transaction", transaction),
            patch("bot.handlers._queue_state_for_update", return_value=state),
            patch("bot.handlers._admin_dashboard_snapshot", return_value=snapshot),
            patch("bot.handlers._pending_video_count", return_value=8),
        ):
            queue_status_command(tg, actor)
        self.assertIn("Filter: Больше", tg.sent[0][1])
        self.assertIn("Filtered pending: 8", tg.sent[0][1])

    def test_reset_repairs_pointer_without_resetting_video_statuses(self) -> None:
        actor = Actor(tg_id=1, chat_id=-1001)
        tg = FakeTelegram()
        conn = MagicMock()

        @contextmanager
        def transaction():
            yield conn

        with (
            patch("bot.handlers.require_superadmin", return_value=True),
            patch("bot.handlers.db.fetch_all", return_value=[]),
            patch("bot.handlers.db.transaction", transaction),
            patch("bot.handlers._queue_state_for_update"),
            patch("bot.handlers._clear_queue_state") as clear,
            patch("bot.handlers.db.log_event"),
            patch(
                "bot.handlers.pump_admin_queue",
                return_value={"pending_count": 0, "active_video_id": None, "active_message_id": None},
            ) as pump,
            patch("bot.handlers.record_system_log"),
            patch("bot.handlers._safe_refresh_admin_dashboard"),
        ):
            reset_admin_queue_command(tg, actor)
        clear.assert_called_once_with(conn)
        pump.assert_called_once_with(tg, actor)
        sql_text = "\n".join(str(call.args[0]) for call in conn.cursor.return_value.__enter__.return_value.execute.call_args_list)
        self.assertNotIn("SET status", sql_text)


class ProfileCursor:
    def __init__(self) -> None:
        self.rows = []
        self.one = None
        self.role_index = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=()):
        if "FROM people WHERE id" in sql:
            self.one = {
                "id": 10,
                "name": "Знамбо",
                "username": "znambo",
                "tg_id": 777,
                "role": "author",
                "is_active": True,
            }
        elif "SELECT id FROM people WHERE tg_id" in sql:
            self.rows = [{"id": 10}, {"id": 11}, {"id": 12}]
        elif "all_count" in sql:
            values = [(128, 9), (57, 4), (11, 2)]
            all_count, month_count = values[self.role_index]
            self.role_index += 1
            self.one = {"all_count": all_count, "month_count": month_count}
        elif "v.status = 'pending'" in sql and "count(*) AS count" in sql:
            self.one = {"count": 4}
        elif "GROUP BY" in sql and "project_code" in sql:
            self.rows = [
                {"project_code": "vzyal_myach", "project_name": "Взял Мяч", "count": 34},
                {"project_code": "bolshe", "project_name": "Больше", "count": 18},
            ]
        elif "LIMIT %s OFFSET %s" in sql:
            self.rows = [
                {
                    "id": 146,
                    "status": "approved",
                    "publish_date": date(2026, 8, 2),
                    "project_name": "Sport Core",
                }
            ]
        else:
            raise AssertionError(sql)

    def fetchone(self):
        return self.one

    def fetchall(self):
        return self.rows


class ProfileConnection:
    def __init__(self) -> None:
        self.cursor_instance = ProfileCursor()

    def cursor(self):
        return self.cursor_instance


@contextmanager
def profile_connection():
    yield ProfileConnection()


class PersonProfileV1014Tests(unittest.TestCase):
    def test_profile_counts_and_last_video(self) -> None:
        with patch("bot.handlers.db.connect", profile_connection):
            profile = load_person_profile(10)
        self.assertIsNotNone(profile)
        text = format_person_profile(profile)
        self.assertIn("Автор — 128", text)
        self.assertIn("Монтаж — 57", text)
        self.assertIn("Озвучка — 11", text)
        self.assertIn("Ожидают проверки: 4", text)
        self.assertIn("#146 — 02.08.2026", text)

    def test_ambiguous_exact_name_returns_choices(self) -> None:
        rows = [
            {"id": 1, "name": "Алексей", "username": "one", "tg_id": 11},
            {"id": 2, "name": "Алексей", "username": "two", "tg_id": 22},
        ]
        with patch("bot.handlers.db.fetch_all", return_value=rows):
            candidates = find_person_candidates("Алексей")
        self.assertEqual([row["id"] for row in candidates], [1, 2])


class SearchV1014Tests(unittest.TestCase):
    def test_exact_video_id_has_first_priority(self) -> None:
        with patch("bot.handlers.db.fetch_all", return_value=[{"id": 61}]) as fetch:
            stage, rows = find_videos_exact("61")
        self.assertEqual(stage, "video_id")
        self.assertEqual(rows[0]["id"], 61)
        self.assertEqual(fetch.call_count, 1)

    def test_instagram_url_uses_exact_shortcode_before_fallback(self) -> None:
        video = {"id": 62, "instagram_id": "DaNTOccMC7z"}
        with patch("bot.handlers.db.fetch_all", return_value=[video]) as fetch:
            stage, rows = find_videos_exact("https://www.instagram.com/reel/DaNTOccMC7z/")
        self.assertEqual(stage, "instagram_id")
        self.assertEqual(rows, [video])
        sql, params = fetch.call_args.args
        self.assertIn("v.instagram_id = %s", sql)
        self.assertEqual(params, ("DaNTOccMC7z",))


class DuplicateReportCursor:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=()):
        self.sql = sql

    def fetchone(self):
        return {"telegram_chat_id": -1001, "telegram_message_id": 300}


class DuplicateReportConnection:
    def cursor(self):
        return DuplicateReportCursor()


@contextmanager
def duplicate_report_transaction():
    yield DuplicateReportConnection()


class DailyReportV1014Tests(unittest.TestCase):
    def test_report_day_boundaries_follow_timezone(self) -> None:
        settings = SimpleNamespace(tz=ZoneInfo("Europe/Moscow"))
        with patch("bot.daily_reports.get_settings", return_value=settings):
            start, end = report_day_bounds(date(2026, 8, 2))
        self.assertEqual(start, datetime(2026, 8, 1, 21, 0, tzinfo=timezone.utc))
        self.assertEqual(end, datetime(2026, 8, 2, 21, 0, tzinfo=timezone.utc))

    def test_daily_report_format(self) -> None:
        text = format_daily_report(
            {
                "report_date": date(2026, 8, 2),
                "approved_count": 38,
                "created_count": 44,
                "pending_count": 7,
                "oldest_pending_age_seconds": 14 * 3600,
                "projects": [{"project_code": "bolshe", "project_name": "Больше", "count": 9}],
                "top_roles": {"author": {"name": "Тихонов", "count": 11}},
            }
        )
        self.assertIn("ОТЧЁТ ЗА 02.08.2026", text)
        self.assertIn("Одобрено: 38", text)
        self.assertIn("Самая старая: 14 ч.", text)

    def test_duplicate_report_is_not_sent_twice(self) -> None:
        tg = FakeTelegram()
        with (
            patch("bot.daily_reports.db.transaction", duplicate_report_transaction),
            patch("bot.daily_reports.db.log_event") as log,
        ):
            result = send_daily_report(date(2026, 8, 2), tg=tg)
        self.assertFalse(result["sent"])
        self.assertTrue(result["duplicate"])
        self.assertEqual(tg.sent, [])
        self.assertEqual(log.call_args.kwargs["action"], "daily_report_skipped_duplicate")


if __name__ == "__main__":
    unittest.main()
