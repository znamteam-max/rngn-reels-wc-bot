from __future__ import annotations

import unittest
from contextlib import contextmanager
from datetime import date
from unittest.mock import MagicMock, patch

from api.health import _egor_montage_debug, _missing_publish_date_debug
from bot.handlers import (
    Actor,
    NEW_DATE_INVALID_MESSAGE,
    NEW_DATE_MANUAL_PROMPT,
    ask_people,
    ask_submission_date,
    bulk_return_missing_dates,
    handle_new_date,
    normalized_submission_data,
    restore_missing_date,
    return_missing_dates_command,
    show_my_requests,
    start_missing_date_revision,
    start_new_manual_date,
)
from bot.people_seeds import seed_and_backfill_egor


class FakeTelegram:
    def __init__(self, fail_chat_ids: set[int] | None = None) -> None:
        self.fail_chat_ids = fail_chat_ids or set()
        self.sent: list[tuple[int, str, object]] = []

    def send_message(self, chat_id, text, reply_markup=None, disable_web_page_preview=True):
        if int(chat_id) in self.fail_chat_ids:
            raise RuntimeError("delivery failed")
        self.sent.append((int(chat_id), text, reply_markup))
        return {"ok": True, "result": {"message_id": 700}}


class AuthorDateFlowTests(unittest.TestCase):
    def test_date_picker_and_manual_state_preserve_project_and_platform(self) -> None:
        actor = Actor(tg_id=1, chat_id=1, username="author")
        tg = FakeTelegram()
        data = {
            "video_type": "regular",
            "instagram_id": "ABC",
            "project_code": "bolshe",
            "project_name": "Больше",
        }
        with patch("bot.handlers.db.set_session") as set_session:
            ask_submission_date(tg, actor, data)
        self.assertEqual(set_session.call_args.kwargs["state"], "new:date")
        self.assertEqual(set_session.call_args.kwargs["data"]["instagram_id"], "ABC")
        keyboard = tg.sent[-1][2]["inline_keyboard"]
        self.assertEqual(
            [button["callback_data"] for row in keyboard for button in row],
            ["newdate:today", "newdate:yesterday", "newdate:manual"],
        )

        with (
            patch("bot.handlers.db.get_session", return_value={"state": "new:date", "data": data}),
            patch("bot.handlers.db.set_session") as set_manual,
        ):
            start_new_manual_date(tg, actor)
        self.assertEqual(set_manual.call_args.kwargs["state"], "new:date_manual")
        self.assertIs(set_manual.call_args.kwargs["data"], data)
        self.assertEqual(tg.sent[-1][1], NEW_DATE_MANUAL_PROMPT)

    def test_manual_date_is_stored_before_author(self) -> None:
        actor = Actor(tg_id=1, chat_id=1, username="author")
        tg = FakeTelegram()
        data = {"project_code": "bolshe", "project_name": "Больше", "instagram_id": "ABC"}
        with (
            patch(
                "bot.handlers.db.get_session",
                return_value={"state": "new:date_manual", "data": data},
            ),
            patch("bot.handlers.parse_publish_date", return_value=date(2026, 8, 3)),
            patch("bot.handlers.db.set_session") as set_session,
            patch("bot.handlers.ask_people") as ask_people_mock,
        ):
            handle_new_date(tg, actor, "03.08")
        self.assertEqual(set_session.call_args.kwargs["state"], "new:author")
        self.assertEqual(set_session.call_args.kwargs["data"]["publish_date"], "2026-08-03")
        ask_people_mock.assert_called_once_with(tg, actor, "author")

    def test_invalid_date_keeps_manual_session_active(self) -> None:
        actor = Actor(tg_id=1, chat_id=1)
        tg = FakeTelegram()
        with (
            patch(
                "bot.handlers.db.get_session",
                return_value={"state": "new:date_manual", "data": {"project_code": "bolshe"}},
            ),
            patch("bot.handlers.db.set_session") as set_session,
        ):
            handle_new_date(tg, actor, "32.08")
        set_session.assert_not_called()
        self.assertEqual(tg.sent[-1][1], NEW_DATE_INVALID_MESSAGE)

    def test_submission_data_requires_date_and_preserves_bigrecap_contract(self) -> None:
        base = {
            "video_type": "bigrecap",
            "project_code": "bolshe",
            "project_name": "Больше",
            "youtube_url": "https://youtu.be/abcdefghijk",
            "youtube_id": "abcdefghijk",
        }
        with self.assertRaisesRegex(ValueError, "publish_date is required"):
            normalized_submission_data(base)
        normalized = normalized_submission_data({**base, "publish_date": "2026-08-03"})
        self.assertEqual(normalized["publish_date"], "2026-08-03")
        self.assertIsNone(normalized["instagram_id"])
        self.assertIsNone(normalized["tiktok_id"])


class MissingDateReturnTests(unittest.TestCase):
    def test_command_only_confirms_before_bulk_change(self) -> None:
        actor = Actor(tg_id=10, chat_id=10, username="admin")
        tg = FakeTelegram()
        with (
            patch("bot.handlers.require_admin", return_value=True),
            patch("bot.handlers.db.fetch_one", return_value={"count": 5}),
            patch("bot.handlers.bulk_return_missing_dates") as bulk,
        ):
            return_missing_dates_command(tg, actor)
        bulk.assert_not_called()
        self.assertIn("Найдено заявок без даты: 5", tg.sent[-1][1])
        callbacks = [
            button["callback_data"]
            for row in tg.sent[-1][2]["inline_keyboard"]
            for button in row
        ]
        self.assertEqual(callbacks, ["missingdate:return", "missingdate:cancel"])

    def test_bulk_starts_durable_operation_without_mass_sends(self) -> None:
        actor = Actor(tg_id=10, chat_id=-1001, username="admin")
        tg = FakeTelegram()
        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.side_effect = [
            None,
            {"count": 5},
            {"id": 7, "total_count": 5, "status": "queued"},
        ]

        @contextmanager
        def transaction():
            yield conn

        with (
            patch("bot.handlers.db.transaction", transaction),
            patch("bot.handlers.db.log_event") as log,
            patch("bot.handlers.jobs.enqueue_job") as enqueue,
        ):
            result = bulk_return_missing_dates(tg, actor)

        self.assertEqual(result["operation_id"], 7)
        self.assertEqual(result["returned_count"], 0)
        self.assertEqual(result["total_count"], 5)
        enqueue.assert_called_once()
        self.assertEqual(enqueue.call_args.args[0], "bulk_return_missing_dates")
        self.assertEqual(log.call_args.kwargs["action"], "bulk_operation_started")
        self.assertEqual(len(tg.sent), 1)
        self.assertIn("Операция #7", tg.sent[0][1])

    def test_owner_restores_only_date_and_fifo_is_repaired(self) -> None:
        actor = Actor(tg_id=101, chat_id=101, username="owner")
        tg = FakeTelegram()
        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = {
            "id": 42,
            "status": "needs_revision",
            "publish_date": None,
            "added_by_tg_id": 101,
            "batch_id": 2,
        }

        @contextmanager
        def transaction():
            yield conn

        state = {"active_video_id": 50, "active_chat_id": -1001, "active_message_id": 900}
        video = {
            "id": 42,
            "status": "pending",
            "publish_date": date(2026, 8, 3),
            "added_by_tg_id": 101,
        }
        with (
            patch("bot.handlers.is_admin", return_value=False),
            patch("bot.handlers.db.transaction", transaction),
            patch("bot.handlers._queue_state_for_update", return_value=state),
            patch("bot.handlers.recalculate_batch") as recalculate,
            patch("bot.handlers.db.log_event") as log,
            patch("bot.handlers._oldest_pending_video", return_value={"id": 42}),
            patch("bot.handlers._clear_queue_state") as clear,
            patch("bot.handlers.get_video_by_id", return_value=video),
            patch("bot.handlers.db.clear_session"),
            patch("bot.handlers._archive_queue_message") as archive,
            patch("bot.handlers.refresh_dashboard_live_or_enqueue"),
            patch("bot.handlers.repair_queue_live_or_enqueue"),
        ):
            restored = restore_missing_date(tg, actor, 42, date(2026, 8, 3))

        self.assertEqual(restored, video)
        recalculate.assert_called_once_with(conn, 2)
        clear.assert_called_once_with(conn)
        archive.assert_called_once()
        self.assertIn(
            "missing_date_revision_submitted",
            [call.kwargs.get("action") for call in log.call_args_list],
        )
        update_sql = str(cursor.execute.call_args_list[1].args[0])
        self.assertIn("status = 'pending'", update_sql)
        self.assertNotIn("project_name", update_sql)
        self.assertIn("Дата: 03.08.2026", tg.sent[-1][1])

    def test_another_normal_user_cannot_open_revision(self) -> None:
        actor = Actor(tg_id=202, chat_id=202)
        tg = FakeTelegram()
        with (
            patch(
                "bot.handlers.get_video_by_id_outside",
                return_value={
                    "id": 42,
                    "status": "needs_revision",
                    "publish_date": None,
                    "added_by_tg_id": 101,
                },
            ),
            patch("bot.handlers.is_admin", return_value=False),
        ):
            start_missing_date_revision(tg, actor, 42)
        self.assertEqual(tg.sent[-1][1], "Можно указывать дату только в своей заявке.")

    def test_my_requests_shows_date_button_without_full_reentry(self) -> None:
        actor = Actor(tg_id=101, chat_id=101)
        tg = FakeTelegram()
        row = {
            "id": 42,
            "status": "needs_revision",
            "publish_date": None,
            "added_by_tg_id": 101,
            "project_name": "Больше",
        }
        with patch("bot.handlers.db.fetch_all", return_value=[row]):
            show_my_requests(tg, actor)
        callbacks = [
            button["callback_data"]
            for keyboard_row in tg.sent[-1][2]["inline_keyboard"]
            for button in keyboard_row
        ]
        self.assertIn("revdate:42", callbacks)
        self.assertNotIn("revise:42", callbacks)


class EgorSeedCursor:
    def __init__(self, conn: "EgorSeedConnection") -> None:
        self.conn = conn
        self.one = None
        self.many: list[tuple[int]] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql: str, params=()):
        compact = " ".join(sql.split())
        if compact.startswith("SELECT id FROM people"):
            matches = [
                person
                for person in self.conn.people
                if person["role"] == "montage"
                and (
                    person["name"].casefold() == "Егор Петрушков".casefold()
                    or (person.get("username") or "").casefold() == "RayBallPro".casefold()
                )
            ]
            self.many = [(person["id"],) for person in matches]
        elif compact.startswith("UPDATE people SET name"):
            person = next(item for item in self.conn.people if item["id"] == int(params[-1]))
            person.update(name=params[0], username=params[1], is_active=True)
        elif compact.startswith("UPDATE people SET is_active = false"):
            for person in self.conn.people:
                if person["id"] in params[0] and person["role"] == "montage":
                    person["is_active"] = False
        elif compact.startswith("INSERT INTO people"):
            person_id = max((person["id"] for person in self.conn.people), default=0) + 1
            self.conn.people.append(
                {
                    "id": person_id,
                    "name": params[0],
                    "username": params[1],
                    "role": params[2],
                    "is_active": True,
                }
            )
            self.one = (person_id,)
        elif compact.startswith("SELECT count(*) FROM people"):
            count = sum(
                1
                for person in self.conn.people
                if person["role"] == "montage"
                and person["name"] == "Егор Петрушков"
                and person.get("username") == "RayBallPro"
                and person["is_active"]
            )
            self.one = (count,)
        elif compact.startswith("UPDATE videos SET montage_username"):
            changed = []
            for video in self.conn.videos:
                if video["montage_name"] == params[2] and not video.get("montage_username"):
                    video["montage_username"] = params[0]
                    video["montage_id"] = params[1]
                    changed.append((video["id"],))
            self.many = changed
        elif compact.startswith("INSERT INTO logs"):
            return
        else:
            raise AssertionError(compact)

    def fetchone(self):
        return self.one

    def fetchall(self):
        return self.many


class EgorSeedConnection:
    def __init__(self) -> None:
        self.people = [
            {"id": 1, "name": "Егор", "username": "egor_author", "role": "author", "is_active": True}
        ]
        self.videos = [
            {"id": 1, "montage_name": "Егор Петрушков", "montage_username": None},
            {"id": 2, "montage_name": "Егор Петрушков", "montage_username": ""},
            {"id": 3, "montage_name": "Другой", "montage_username": None},
            {"id": 4, "montage_name": "Егор Петрушков", "montage_username": "other"},
        ]

    def cursor(self):
        return EgorSeedCursor(self)


class EgorMontageTests(unittest.TestCase):
    def test_seed_is_role_specific_idempotent_and_backfill_is_exact(self) -> None:
        conn = EgorSeedConnection()
        first = seed_and_backfill_egor(conn)
        second = seed_and_backfill_egor(conn)
        active = [
            person
            for person in conn.people
            if person["role"] == "montage" and person["is_active"]
        ]
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["name"], "Егор Петрушков")
        self.assertEqual(active[0]["username"], "RayBallPro")
        self.assertEqual(conn.people[0]["name"], "Егор")
        self.assertEqual(first["backfilled_count"], 2)
        self.assertEqual(second["backfilled_count"], 0)
        self.assertIsNone(conn.videos[2].get("montage_username"))
        self.assertEqual(conn.videos[3]["montage_username"], "other")

    def test_montage_picker_displays_egor_username(self) -> None:
        actor = Actor(tg_id=1, chat_id=1)
        tg = FakeTelegram()
        with (
            patch(
                "bot.handlers.get_people",
                return_value=[{"id": 9, "name": "Егор Петрушков", "username": "RayBallPro"}],
            ),
            patch("bot.handlers.db.get_session", return_value={"data": {}}),
        ):
            ask_people(tg, actor, "montage")
        labels = [
            button["text"]
            for row in tg.sent[-1][2]["inline_keyboard"]
            for button in row
        ]
        self.assertIn("Егор Петрушков (@RayBallPro)", labels)

    def test_health_diagnostics_have_counts_without_ids(self) -> None:
        with patch("api.health.db.fetch_one", return_value={"pending": 5, "needs_revision": 2}):
            missing = _missing_publish_date_debug()
        with patch("api.health.db.fetch_one", return_value={"active_rows": 1, "backfilled_videos": 2}):
            egor = _egor_montage_debug()
        self.assertEqual(missing, {"pending": 5, "needs_revision": 2})
        self.assertEqual(egor, {"active_rows": 1, "backfilled_videos": 2})
        self.assertEqual(set(egor), {"active_rows", "backfilled_videos"})


if __name__ == "__main__":
    unittest.main()
