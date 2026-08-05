from __future__ import annotations

import inspect
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch

from bot import admin_queue, handlers, job_worker
from bot.admin_queue import CompletionResult, QueueDeliveryResult, QueueReservation
from bot.handlers import Actor


SETTINGS = SimpleNamespace(
    admin_chat_id=-1001,
    bot_token="",
    database_url="",
    cron_secret="",
    google_service_account_json_b64="",
)


def reservation(*, deliver: bool = True, message_id: int | None = None) -> QueueReservation:
    return QueueReservation(
        queue_name="main",
        video_id=41,
        chat_id=-1001,
        token="token-41",
        generation=7,
        reserved_at=datetime.now(timezone.utc),
        delivery_attempts=1,
        reason="test",
        should_deliver=deliver,
        active_message_id=message_id,
        pending_count=2,
        global_pending_count=2,
    )


class FakeTelegram:
    def __init__(self, *, send_error: Exception | None = None, edit_error: Exception | None = None):
        self.send_error = send_error
        self.edit_error = edit_error
        self.sent = 0
        self.events: list[str] = []

    def send_message(self, *_args, **_kwargs):
        self.events.append("send")
        self.sent += 1
        if self.send_error:
            raise self.send_error
        return {"ok": True, "result": {"message_id": 501}}

    def answer_callback_query(self, *_args, **_kwargs):
        self.events.append("answer")

    def edit_message_text(self, *_args, **_kwargs):
        self.events.append("edit")
        if self.edit_error:
            raise self.edit_error
        return {"ok": True, "result": {"message_id": 500}}


class AtomicQueueV1018Tests(TestCase):
    def _conn(self, fetchone_rows: list[dict]) -> tuple[MagicMock, MagicMock]:
        conn = MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.side_effect = fetchone_rows
        cur.fetchall.return_value = []
        return conn, cur

    def test_pump_commits_reservation_before_delivery(self) -> None:
        events: list[str] = []

        @contextmanager
        def transaction():
            events.append("transaction_enter")
            yield MagicMock()
            events.append("transaction_commit")

        def deliver(*_args, **_kwargs):
            events.append("telegram_delivery")
            return QueueDeliveryResult(41, True, True, 501)

        with (
            patch("bot.admin_queue.db.transaction", transaction),
            patch("bot.admin_queue.reserve_next_pending_card", return_value=reservation()),
            patch("bot.admin_queue.deliver_reserved_card", side_effect=deliver),
        ):
            result = admin_queue.pump_queue_live(FakeTelegram(), reason="ordering")
        self.assertTrue(result["pointer_saved"])
        self.assertLess(events.index("transaction_commit"), events.index("telegram_delivery"))

    def test_recent_reservation_prevents_duplicate_selection(self) -> None:
        state = {
            "queue_name": "main",
            "active_video_id": 41,
            "active_chat_id": -1001,
            "active_message_id": None,
            "active_reservation_token": "token-41",
            "active_reserved_at": datetime.now(timezone.utc),
            "active_generation": 7,
            "active_delivery_attempts": 1,
            "queue_filter_type": "global",
        }
        conn, cur = self._conn(
            [state, {"count": 2}, {"count": 2}, {"id": 41, "status": "pending"}]
        )
        with patch("bot.admin_queue.get_settings", return_value=SETTINGS):
            result = admin_queue.reserve_next_pending_card(conn, reason="concurrent_pump")
        self.assertIsNotNone(result)
        self.assertFalse(result.should_deliver)
        sql = "\n".join(str(call.args[0]) for call in cur.execute.call_args_list)
        self.assertNotIn("FOR UPDATE SKIP LOCKED", sql)

    def test_stale_reservation_retries_same_video(self) -> None:
        state = {
            "queue_name": "main",
            "active_video_id": 41,
            "active_chat_id": -1001,
            "active_message_id": None,
            "active_reservation_token": "old-token",
            "active_reserved_at": datetime.now(timezone.utc) - timedelta(seconds=6),
            "active_generation": 7,
            "active_delivery_attempts": 1,
            "queue_filter_type": "global",
        }
        reserved = {
            **state,
            "active_reservation_token": "new-token",
            "active_reserved_at": datetime.now(timezone.utc),
            "active_generation": 8,
            "active_delivery_attempts": 2,
        }
        conn, cur = self._conn(
            [state, {"count": 2}, {"count": 2}, {"id": 41, "status": "pending"}, reserved]
        )
        with (
            patch("bot.admin_queue.get_settings", return_value=SETTINGS),
            patch("bot.admin_queue.db.log_event"),
        ):
            result = admin_queue.reserve_next_pending_card(conn, reason="watchdog")
        self.assertEqual(result.video_id, 41)
        self.assertTrue(result.should_deliver)
        sql = "\n".join(str(call.args[0]) for call in cur.execute.call_args_list)
        self.assertNotIn("FOR UPDATE SKIP LOCKED", sql)

    def test_invalid_active_status_is_cleared_and_oldest_reserved(self) -> None:
        state = {
            "queue_name": "main",
            "active_video_id": 40,
            "active_chat_id": -1001,
            "active_message_id": 499,
            "active_generation": 3,
            "active_delivery_attempts": 1,
            "queue_filter_type": "global",
        }
        reserved = {
            **state,
            "active_video_id": 41,
            "active_message_id": None,
            "active_reservation_token": "new-token",
            "active_reserved_at": datetime.now(timezone.utc),
            "active_generation": 4,
            "active_delivery_attempts": 2,
        }
        conn, cur = self._conn(
            [
                state,
                {"count": 2},
                {"count": 2},
                {"id": 40, "status": "approved"},
                {"id": 41},
                reserved,
            ]
        )
        with (
            patch("bot.admin_queue.get_settings", return_value=SETTINGS),
            patch("bot.admin_queue.db.log_event"),
        ):
            result = admin_queue.reserve_next_pending_card(conn, reason="invalid_pointer")
        self.assertEqual(result.video_id, 41)
        sql = "\n".join(str(call.args[0]) for call in cur.execute.call_args_list)
        self.assertIn("FOR UPDATE SKIP LOCKED", sql)
        self.assertNotIn("admin_message_id IS NULL", sql)

    def test_send_failure_releases_matching_reservation(self) -> None:
        tg = FakeTelegram(send_error=RuntimeError("telegram unavailable"))

        @contextmanager
        def connection():
            yield MagicMock()

        with (
            patch("bot.admin_queue._load_delivery_payload", return_value=({"status": "pending"}, "x", {})),
            patch("bot.admin_queue.db.connect", connection),
            patch("bot.admin_queue._release_failed_reservation", return_value=17) as release,
            patch("bot.admin_queue._log_standalone"),
            patch("bot.admin_queue.get_settings", return_value=SETTINGS),
        ):
            result = admin_queue.deliver_reserved_card(tg, reservation())
        self.assertFalse(result.sent)
        self.assertEqual(result.repair_job_id, 17)
        release.assert_called_once()

    def test_pointer_save_failure_enqueues_message_adoption(self) -> None:
        conn = MagicMock()

        @contextmanager
        def transaction():
            yield conn

        @contextmanager
        def connection():
            yield conn

        with (
            patch("bot.admin_queue._load_delivery_payload", return_value=({"status": "pending"}, "x", {})),
            patch("bot.admin_queue.db.connect", connection),
            patch("bot.admin_queue.db.transaction", transaction),
            patch("bot.admin_queue._save_pointer_message", side_effect=RuntimeError("db save failed")),
            patch("bot.admin_queue.jobs.enqueue_admin_queue_pump", return_value=23) as enqueue,
            patch("bot.admin_queue._log_standalone"),
            patch("bot.admin_queue.get_settings", return_value=SETTINGS),
        ):
            result = admin_queue.deliver_reserved_card(FakeTelegram(), reservation())
        self.assertTrue(result.sent)
        self.assertFalse(result.pointer_saved)
        payload = enqueue.call_args.kwargs["adopt_message"]
        self.assertEqual(payload["message_id"], 501)
        self.assertEqual(payload["reservation_token"], "token-41")

    def test_two_callbacks_accept_only_first(self) -> None:
        conn, cur = self._conn(
            [
                {"active_video_id": 41, "active_chat_id": -1001, "active_message_id": 500, "active_generation": 7},
                {"id": 41, "status": "pending"},
            ]
        )
        mutation = MagicMock(return_value={"id": 41, "status": "approved"})
        with patch("bot.admin_queue.db.log_event"):
            first = admin_queue._complete_active_action_in_conn(
                conn,
                callback_chat_id=-1001,
                callback_message_id=500,
                video_id=41,
                actor=SimpleNamespace(tg_id=1, username="admin"),
                action="approved",
                mutation=mutation,
                queue_name="main",
            )
            cur.fetchone.side_effect = [
                {"active_video_id": None, "active_chat_id": None, "active_message_id": None, "active_generation": 7},
                {"id": 41, "status": "approved"},
            ]
            second = admin_queue._complete_active_action_in_conn(
                conn,
                callback_chat_id=-1001,
                callback_message_id=500,
                video_id=41,
                actor=SimpleNamespace(tg_id=2, username="admin2"),
                action="duplicate",
                mutation=mutation,
                queue_name="main",
            )
        self.assertTrue(first.accepted)
        self.assertFalse(second.accepted)
        mutation.assert_called_once()

    def test_two_pumps_send_one_card(self) -> None:
        tg = FakeTelegram()

        @contextmanager
        def transaction():
            yield MagicMock()

        @contextmanager
        def connection():
            yield MagicMock()

        with (
            patch("bot.admin_queue.db.transaction", transaction),
            patch("bot.admin_queue.db.connect", connection),
            patch(
                "bot.admin_queue.reserve_next_pending_card",
                side_effect=[reservation(), reservation(deliver=False, message_id=501)],
            ),
            patch("bot.admin_queue._load_delivery_payload", return_value=({"status": "pending"}, "x", {})),
            patch("bot.admin_queue._save_pointer_message", return_value=True),
            patch("bot.admin_queue._log_standalone"),
        ):
            first = admin_queue.pump_queue_live(tg, reason="pump_one")
            second = admin_queue.pump_queue_live(tg, reason="pump_two")
        self.assertTrue(first["sent"])
        self.assertFalse(second["sent"])
        self.assertEqual(tg.sent, 1)

    def test_missing_active_pointer_auto_pumps(self) -> None:
        conn, cur = self._conn(
            [
                {"queue_name": "main", "active_video_id": None, "queue_filter_type": "global"},
                {"count": 1},
                {"count": 1},
            ]
        )

        @contextmanager
        def transaction():
            yield conn

        with (
            patch("bot.admin_queue.db.transaction", transaction),
            patch("bot.admin_queue.db.log_event"),
            patch("bot.admin_queue.pump_queue_live", return_value={"active_video_id": 41, "active_message_id": 501}) as pump,
        ):
            result = admin_queue.repair_queue_if_needed(FakeTelegram(), reason="missing")
        self.assertTrue(result.pump_needed)
        pump.assert_called_once()
        self.assertEqual(cur.fetchall.call_count, 1)

    def test_dashboard_and_other_modules_cannot_write_active_fields(self) -> None:
        dashboard_source = inspect.getsource(handlers._refresh_admin_dashboard_with_conn)
        self.assertNotIn("SET active_", dashboard_source)
        self.assertNotIn("pump_queue", dashboard_source)
        self.assertNotIn("admin_message_id", dashboard_source)
        worker_source = inspect.getsource(job_worker._handle_dashboard_refresh)
        self.assertNotIn("admin_queue", worker_source)
        self.assertNotIn("pump", worker_source)

        root = Path(__file__).resolve().parents[1]
        allowed = {root / "bot" / "admin_queue.py", root / "bot" / "runtime_migrations.py"}
        offenders = []
        for path in [*(root / "bot").glob("*.py"), *(root / "api").glob("*.py")]:
            if path in allowed:
                continue
            if "SET active_" in path.read_text(encoding="utf-8"):
                offenders.append(path.name)
        self.assertEqual(offenders, [])

    def test_old_card_and_dashboard_failures_do_not_stop_next_pump(self) -> None:
        tg = FakeTelegram(edit_error=RuntimeError("old card unavailable"))
        actor = Actor(tg_id=1, chat_id=-1001, username="admin")
        completion = CompletionResult(
            accepted=True,
            video_id=41,
            action="duplicate",
            video={"id": 41, "status": "duplicate", "project_name": "Test"},
            old_active_video_id=41,
            generation=7,
            timings_ms={"commit_ms": 3},
        )
        order: list[str] = []
        with (
            patch("bot.handlers.admin_queue.complete_active_action", return_value=completion),
            patch("bot.handlers.record_system_log"),
            patch("bot.handlers.pump_queue_live_or_enqueue", side_effect=lambda *_a, **_k: order.append("pump") or {"active_video_id": 42}),
            patch("bot.handlers.jobs.enqueue_dashboard_refresh", side_effect=lambda: order.append("dashboard") or (_ for _ in ()).throw(RuntimeError("dashboard failed"))),
        ):
            error, answered = handlers._process_admin_queue_action_v1018(
                tg, actor, 41, 500, "duplicate", "callback"
            )
        self.assertIsNone(error)
        self.assertTrue(answered)
        self.assertEqual(order, ["pump", "dashboard"])

    def test_non_final_admin_callback_is_answered_without_unbound_state(self) -> None:
        tg = FakeTelegram()
        actor = Actor(tg_id=1, chat_id=-1001, username="admin")
        with (
            patch("bot.handlers.is_admin", return_value=True),
            patch("bot.handlers._show_admin_queue_date_options", return_value=None),
        ):
            handlers.handle_admin_queue_callback(tg, actor, "admq:date:41", 500, "callback")
        self.assertEqual(tg.events, ["answer"])

    def test_first_job_error_survives_success_sql(self) -> None:
        conn = MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value

        @contextmanager
        def transaction():
            yield conn

        job = {"id": 9, "kind": "dashboard_refresh", "attempts": 1, "max_attempts": 3, "payload": {}}
        with (
            patch("bot.job_worker.db.transaction", transaction),
            patch("bot.job_worker.db.log_event"),
            patch("bot.job_worker.get_settings", return_value=SETTINGS),
        ):
            job_worker._fail_job(job, RuntimeError("first failure"))
        failure_sql = str(cur.execute.call_args_list[0].args[0])
        self.assertIn("first_error = COALESCE(first_error", failure_sql)
        self.assertIn("failure_count = failure_count + 1", failure_sql)
        self.assertEqual(cur.execute.call_args_list[0].args[1][0], "failed")

        cur.execute.reset_mock()
        with (
            patch("bot.job_worker.db.transaction", transaction),
            patch("bot.job_worker.db.log_event"),
        ):
            job_worker._finish_job(job, 10)
        success_sql = str(cur.execute.call_args_list[0].args[0])
        self.assertNotIn("first_error", success_sql)
        self.assertNotIn("failure_count", success_sql)

    def test_filters_keep_v1017_semantics(self) -> None:
        self.assertEqual(admin_queue.queue_filter_sql({"queue_filter_type": "global"}), ("TRUE", ()))
        self.assertEqual(
            admin_queue.queue_filter_sql({"queue_filter_type": "project", "queue_filter_value": "bolshe"}),
            ("v.project_code = %s", ("bolshe",)),
        )
        self.assertEqual(
            admin_queue.queue_filter_sql({"queue_filter_type": "other"}),
            ("v.project_code = 'other'", ()),
        )
        condition, params = admin_queue.queue_filter_sql({"queue_filter_type": "unassigned"})
        self.assertIn("COALESCE(v.project_code", condition)
        self.assertEqual(params, ())

    def test_migration_repairs_only_non_active_pending_metadata(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "bot" / "runtime_migrations.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("AND (%s::bigint IS NULL OR id <> %s)", source)
        self.assertIn("AND admin_message_id IS NOT NULL", source)
        self.assertNotIn("/return_missing_dates", source)

    def test_nullable_sql_parameters_have_explicit_postgres_types(self) -> None:
        root = Path(__file__).resolve().parents[1]
        source = "\n".join(
            (root / path).read_text(encoding="utf-8")
            for path in ("bot/admin_queue.py", "bot/runtime_migrations.py")
        )
        self.assertNotIn("%s IS NULL", source)
        self.assertNotIn("%s IS NOT NULL", source)
