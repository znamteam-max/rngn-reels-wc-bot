from __future__ import annotations

import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from bot.handlers import (
    ADD_ZNAMBO_SESSION_PROJECT,
    Actor,
    PROJECT_OTHER_INVALID_MESSAGE,
)
from bot.handlers import (
    admin_approval_error,
    ask_add_znambo_date,
    format_admin_dashboard,
    handle_project_other_message,
    handle_project_pick,
    notify_admin_queue,
    project_picker_keyboard,
    refresh_admin_dashboard,
)
from bot.projects import PROJECTS, normalize_custom_project_name, seed_projects
from bot.sheets import (
    SHEET_COLUMNS,
    _sync_video_project_sheet,
    _upsert_video_in_named_sheet,
    build_people_projects_rows,
    build_project_stats_rows,
)
from bot.telegram import TelegramAPIError
from scripts.init_db import SCHEMA_SQL


class FakeTelegram:
    def __init__(self, *, deleted_dashboard: bool = False) -> None:
        self.deleted_dashboard = deleted_dashboard
        self.sent: list[tuple[int, str, object]] = []
        self.edited: list[tuple[int, int, str, object]] = []
        self.pinned: list[tuple[int, int, bool]] = []

    def send_message(self, chat_id, text, reply_markup=None, disable_web_page_preview=True):
        self.sent.append((int(chat_id), text, reply_markup))
        return {"ok": True, "result": {"message_id": 300}}

    def edit_message_text(self, chat_id, message_id, text, reply_markup=None, disable_web_page_preview=True):
        if self.deleted_dashboard:
            raise TelegramAPIError("Bad Request: message to edit not found", 400)
        self.edited.append((int(chat_id), int(message_id), text, reply_markup))
        return {"ok": True}

    def pin_chat_message(self, chat_id, message_id, disable_notification=True):
        self.pinned.append((int(chat_id), int(message_id), bool(disable_notification)))
        return {"ok": True}


class SeedCursor:
    def __init__(self, rows: dict[str, tuple[object, ...]]) -> None:
        self.rows = rows
        self.result = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql: str, params=()):
        if "INSERT INTO projects" in sql:
            self.rows[str(params[0])] = tuple(params)
        elif "count(*) FROM projects" in sql:
            self.result = (len(self.rows),)

    def fetchone(self):
        return self.result


class SeedConnection:
    def __init__(self) -> None:
        self.rows: dict[str, tuple[object, ...]] = {}

    def cursor(self):
        return SeedCursor(self.rows)


class DashboardCursor:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql: str, params=()):
        return None


class DashboardConnection:
    def cursor(self):
        return DashboardCursor()


@contextmanager
def dashboard_transaction():
    yield DashboardConnection()


class ProjectsV1013Tests(unittest.TestCase):
    def test_existing_database_adds_project_columns_before_indexes(self) -> None:
        alter_position = SCHEMA_SQL.index("ALTER TABLE videos ADD COLUMN IF NOT EXISTS project_id")
        project_index_position = SCHEMA_SQL.index("CREATE INDEX IF NOT EXISTS idx_videos_project_id")
        status_index_position = SCHEMA_SQL.index("CREATE INDEX IF NOT EXISTS idx_videos_status_project")
        self.assertLess(alter_position, project_index_position)
        self.assertLess(alter_position, status_index_position)

    def test_repeated_project_seed_has_nine_unique_active_projects(self) -> None:
        conn = SeedConnection()
        self.assertEqual(seed_projects(conn), 9)
        self.assertEqual(seed_projects(conn), 9)
        self.assertEqual(len(conn.rows), 9)
        self.assertEqual(len({project["code"] for project in PROJECTS}), 9)

    def test_project_picker_has_exact_nine_callbacks(self) -> None:
        keyboard = project_picker_keyboard()["inline_keyboard"]
        callbacks = [button["callback_data"] for row in keyboard for button in row]
        self.assertEqual(callbacks, [f"proj:{project['code']}" for project in PROJECTS])

    def test_project_pick_continues_regular_flow_to_author(self) -> None:
        actor = Actor(tg_id=1, chat_id=1, username="user")
        tg = FakeTelegram()
        data = {"video_type": "regular", "instagram_id": "ABC"}
        with (
            patch("bot.handlers.db.get_session", return_value={"state": "new:project", "data": data}),
            patch(
                "bot.handlers.get_active_project",
                return_value={"id": 2, "code": "bolshe", "name": "Больше"},
            ),
            patch("bot.handlers.db.set_session") as set_session,
            patch("bot.handlers.ask_people") as ask_people,
        ):
            handle_project_pick(tg, actor, "bolshe")
        self.assertEqual(set_session.call_args.kwargs["state"], "new:author")
        self.assertEqual(set_session.call_args.kwargs["data"]["project_name"], "Больше")
        ask_people.assert_called_once_with(tg, actor, "author")

    def test_project_pick_continues_znambo_flow_to_date(self) -> None:
        actor = Actor(tg_id=1, chat_id=1, username="znambo")
        tg = FakeTelegram()
        data = {"flow": "add_znambo", "instagram_id": "ABC"}
        with (
            patch(
                "bot.handlers.db.get_session",
                return_value={"state": ADD_ZNAMBO_SESSION_PROJECT, "data": data},
            ),
            patch(
                "bot.handlers.get_active_project",
                return_value={"id": 1, "code": "vzyal_myach", "name": "Взял Мяч"},
            ),
            patch("bot.handlers.ask_add_znambo_date") as ask_date,
        ):
            handle_project_pick(tg, actor, "vzyal_myach")
        ask_date.assert_called_once()
        self.assertEqual(ask_date.call_args.args[2]["project_code"], "vzyal_myach")

    def test_other_project_stores_snapshot_without_id(self) -> None:
        actor = Actor(tg_id=1, chat_id=1, username="user")
        tg = FakeTelegram()
        data = {"video_type": "regular"}
        with patch("bot.handlers._continue_after_project") as continue_flow:
            handle_project_other_message(tg, actor, "new:project_other", data, "  Secret   Show  ")
        self.assertEqual(data["project_id"], None)
        self.assertEqual(data["project_code"], "other")
        self.assertEqual(data["project_name"], "Secret Show")
        continue_flow.assert_called_once()

    def test_other_project_rejects_links_and_invalid_length(self) -> None:
        self.assertIsNone(normalize_custom_project_name("x"))
        self.assertIsNone(normalize_custom_project_name("https://example.com/project"))
        actor = Actor(tg_id=1, chat_id=1)
        tg = FakeTelegram()
        with patch("bot.handlers._continue_after_project") as continue_flow:
            handle_project_other_message(tg, actor, "new:project_other", {}, "x")
        continue_flow.assert_not_called()
        self.assertEqual(tg.sent[0][1], PROJECT_OTHER_INVALID_MESSAGE)

    def test_old_pending_cannot_be_approved_before_project_assignment(self) -> None:
        self.assertEqual(admin_approval_error({"publish_date": "2026-08-03"}), "Сначала укажи проект.")
        self.assertEqual(
            admin_approval_error({"project_code": "bolshe", "project_name": "Больше"}),
            "Сначала укажи дату публикации.",
        )

    def test_add_znambo_date_keyboard_remains_v1012_layout(self) -> None:
        actor = Actor(tg_id=1, chat_id=1)
        tg = FakeTelegram()
        with patch("bot.handlers.db.set_session"):
            ask_add_znambo_date(tg, actor, {"project_code": "bolshe", "project_name": "Больше"})
        keyboard = tg.sent[0][2]["inline_keyboard"]
        self.assertEqual(
            [[button["text"] for button in row] for row in keyboard],
            [["Сегодня", "Вчера"], ["Ввести вручную"]],
        )


class DashboardV1013Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.snapshot = {
            "pending_count": 3,
            "active_video_id": 64,
            "oldest_created_at": datetime.now(timezone.utc),
            "project_counts": [
                {"emoji": "🏀", "name": "Взял Мяч", "count": 2},
                {"emoji": "❓", "name": "Без проекта", "count": 1},
            ],
            "updated_at": datetime(2026, 8, 3, 15, 53, tzinfo=ZoneInfo("Europe/Moscow")),
        }
        self.state = {
            "active_video_id": 64,
            "dashboard_chat_id": -1001,
            "dashboard_message_id": 245,
        }

    def _patch_dashboard(self):
        return (
            patch("bot.handlers.get_settings", return_value=SimpleNamespace(admin_chat_id=-1001, tz=ZoneInfo("Europe/Moscow"))),
            patch("bot.handlers.db.transaction", dashboard_transaction),
            patch("bot.handlers._queue_state_for_update", return_value=self.state),
            patch("bot.handlers._admin_dashboard_snapshot", return_value=self.snapshot),
            patch("bot.handlers.db.log_event"),
        )

    def test_dashboard_edits_one_persistent_message(self) -> None:
        tg = FakeTelegram()
        patches = self._patch_dashboard()
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            first = refresh_admin_dashboard(tg)
            second = refresh_admin_dashboard(tg)
        self.assertEqual(first["message_id"], 245)
        self.assertEqual(second["message_id"], 245)
        self.assertEqual(len(tg.edited), 2)
        self.assertEqual(tg.sent, [])

    def test_deleted_dashboard_is_recreated_and_pinned(self) -> None:
        tg = FakeTelegram(deleted_dashboard=True)
        patches = self._patch_dashboard()
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            result = refresh_admin_dashboard(tg)
        self.assertTrue(result["created"])
        self.assertEqual(result["message_id"], 300)
        self.assertEqual(tg.pinned, [(-1001, 300, True)])

    def test_dashboard_text_contains_total_active_and_projects(self) -> None:
        text = format_admin_dashboard(self.snapshot)
        self.assertIn("🔴 Ждут проверки: 3", text)
        self.assertIn("▶️ Текущая заявка: #64", text)
        self.assertIn("🏀 Взял Мяч — 2", text)
        self.assertIn("❓ Без проекта — 1", text)

    def test_new_pending_with_active_card_refreshes_dashboard_without_new_card(self) -> None:
        tg = FakeTelegram()
        actor = Actor(tg_id=1, chat_id=1)
        with (
            patch("bot.handlers._safe_refresh_admin_dashboard") as refresh,
            patch(
                "bot.handlers.pump_admin_queue",
                return_value={
                    "pending_count": 4,
                    "active_video_id": 64,
                    "active_message_id": 250,
                    "sent": False,
                },
            ) as pump,
            patch("bot.handlers.record_system_log"),
        ):
            self.assertTrue(notify_admin_queue(tg, {"id": 99}, actor))
        self.assertEqual(refresh.call_count, 2)
        pump.assert_called_once()
        self.assertEqual(tg.sent, [])


class ProjectSheetsV1013Tests(unittest.TestCase):
    def test_project_sheet_upsert_is_idempotent(self) -> None:
        service = MagicMock()
        values = service.spreadsheets.return_value.values.return_value
        values.get.return_value.execute.side_effect = [{"values": []}, {"values": [["77"]]}]
        values.append.return_value.execute.return_value = {"updates": {"updatedRange": "'Больше'!A2:Z2"}}
        video = {"id": 77, "project_code": "bolshe", "project_name": "Больше"}
        _upsert_video_in_named_sheet(service, "sheet", "Больше", video, SHEET_COLUMNS)
        _upsert_video_in_named_sheet(service, "sheet", "Больше", video, SHEET_COLUMNS)
        self.assertEqual(values.append.call_count, 1)
        self.assertEqual(values.update.call_count, 1)

    def test_project_change_clears_old_sheets_and_upserts_target_once(self) -> None:
        video = {"id": 77, "project_code": "bolshe", "project_name": "Больше"}
        service = MagicMock()
        with (
            patch("bot.sheets._ensure_named_sheets"),
            patch("bot.sheets._project_sheet_rows_by_id", return_value={"Взял Мяч": 4}),
            patch("bot.sheets._write_video_to_named_sheet") as write,
        ):
            _sync_video_project_sheet(service, "sheet", video, SHEET_COLUMNS)
        clear_body = service.spreadsheets.return_value.values.return_value.batchClear.call_args.kwargs["body"]
        self.assertEqual(len(clear_body["ranges"]), 1)
        self.assertIn("'Взял Мяч'!A4:", clear_body["ranges"][0])
        write.assert_called_once()
        self.assertEqual(write.call_args.args[2], "Больше")
        self.assertIsNone(write.call_args.kwargs["row_number"])

    def test_project_stats_and_people_projects_use_approved_roles(self) -> None:
        videos = [
            {
                "id": 1,
                "status": "approved",
                "project_code": "bolshe",
                "project_name": "Больше",
                "author_name": "Аня",
                "author_username": "anya",
                "montage_name": "Миша",
                "montage_username": "misha",
            },
            {
                "id": 2,
                "status": "pending",
                "project_code": "bolshe",
                "project_name": "Больше",
                "author_name": "Другой",
            },
        ]
        stats = build_project_stats_rows(videos, updated_at=datetime(2026, 8, 3, tzinfo=timezone.utc))
        bolshe = next(row for row in stats if row[0] == "bolshe")
        self.assertEqual(bolshe[2:7], ["1", "1", "0", "0", "1"])
        people = build_people_projects_rows(videos)
        self.assertEqual(len(people), 2)
        self.assertTrue(all(row[2] == "bolshe" for row in people))


if __name__ == "__main__":
    unittest.main()
