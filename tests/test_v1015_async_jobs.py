from __future__ import annotations

import os
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from bot import jobs
from bot.config import get_settings
from bot.handlers import (
    Actor,
    _safe_refresh_admin_dashboard,
    bulk_return_missing_dates,
    sync_video_after_approval,
)
from bot.job_worker import BULK_CHUNK_SIZE, claim_jobs, process_jobs
from bot.projects import PROJECT_SHEET_TITLES, project_sheet_title
from bot.sheets import SHEET_COLUMNS, SHEET_NAME, batch_upsert_videos
from bot.telegram import TelegramAPIError, TelegramClient


class FakeTelegram:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []

    def send_message(self, chat_id: int, text: str, reply_markup=None):
        self.messages.append((chat_id, text))
        return {"ok": True}


class AsyncJobsV1015Tests(unittest.TestCase):
    def tearDown(self) -> None:
        get_settings.cache_clear()

    def test_numeric_flags_and_pool_defaults_are_parsed(self) -> None:
        with patch.dict(
            os.environ,
            {
                "ADMIN_CHAT_ID": "-100123",
                "JOB_WORKER_BATCH_SIZE": "99",
                "JOB_WORKER_TIME_BUDGET_SECONDS": "17",
                "DB_POOL_MAX_SIZE": "4",
                "TELEGRAM_MAX_CONNECTIONS": "5",
                "BACKGROUND_JOBS_ENABLED": "false",
            },
            clear=True,
        ):
            get_settings.cache_clear()
            settings = get_settings()
        self.assertEqual(settings.admin_chat_id, -100123)
        self.assertEqual(settings.job_worker_batch_size, 20)
        self.assertEqual(settings.job_worker_time_budget_seconds, 17)
        self.assertEqual(settings.db_pool_max_size, 4)
        self.assertEqual(settings.telegram_max_connections, 5)
        self.assertFalse(settings.background_jobs_enabled)

    def test_arbitrary_job_kind_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unsupported background job kind"):
            jobs.enqueue_job("os.system", {"command": "nope"})

    def test_telegram_update_new_done_and_stale_reclaim(self) -> None:
        update = {
            "update_id": 123,
            "message": {"from": {"id": 7}, "chat": {"id": 8}, "text": "/start"},
        }
        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value

        @contextmanager
        def transaction():
            yield conn

        with patch("bot.jobs.db.transaction", transaction):
            cursor.fetchone.side_effect = [{"update_id": 123}]
            self.assertEqual(jobs.claim_telegram_update(update), "claimed")

            cursor.fetchone.side_effect = [None, {"status": "done", "processing_started_at": None}]
            self.assertEqual(jobs.claim_telegram_update(update), "duplicate_done")

            cursor.fetchone.side_effect = [
                None,
                {
                    "status": "processing",
                    "processing_started_at": datetime.now(timezone.utc) - timedelta(minutes=6),
                },
            ]
            self.assertEqual(jobs.claim_telegram_update(update), "reclaimed")

    def test_one_hundred_concurrent_updates_and_duplicates_are_idempotent(self) -> None:
        state: dict[int, dict[str, object]] = {}
        lock = threading.Lock()

        class Cursor:
            def __init__(self) -> None:
                self.row = None

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def execute(self, sql, params):
                update_id = int(params[-1] if sql.lstrip().startswith("UPDATE") else params[0])
                if "INSERT INTO telegram_updates" in sql:
                    if update_id in state:
                        self.row = None
                    else:
                        state[update_id] = {
                            "status": "processing",
                            "processing_started_at": datetime.now(timezone.utc),
                        }
                        self.row = {"update_id": update_id}
                elif "SELECT status" in sql:
                    self.row = state[update_id]
                elif "UPDATE telegram_updates" in sql:
                    state[update_id]["processing_started_at"] = datetime.now(timezone.utc)
                    self.row = None

            def fetchone(self):
                return self.row

        class Connection:
            def cursor(self):
                return Cursor()

        @contextmanager
        def transaction():
            with lock:
                yield Connection()

        def claim(update_id: int) -> str:
            return jobs.claim_telegram_update(
                {
                    "update_id": update_id,
                    "message": {"from": {"id": 7}, "chat": {"id": 8}},
                }
            )

        with patch("bot.jobs.db.transaction", transaction):
            with ThreadPoolExecutor(max_workers=20) as executor:
                unique = list(executor.map(claim, range(1, 101)))
                duplicates = list(executor.map(lambda _index: claim(500), range(100)))
        self.assertEqual(unique, ["claimed"] * 100)
        self.assertEqual(duplicates.count("claimed"), 1)
        self.assertEqual(duplicates.count("duplicate_processing"), 99)

    def test_dashboard_burst_uses_one_dedupe_key(self) -> None:
        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.side_effect = [
            {"id": 11, "inserted": index == 0}
            for index in range(50)
        ]
        with patch("bot.jobs.db.log_event") as log:
            for _ in range(50):
                jobs.enqueue_dashboard_refresh(conn=conn)
        params = [call.args[1] for call in cursor.execute.call_args_list]
        self.assertEqual({item[1] for item in params}, {"dashboard:main"})
        self.assertEqual(log.call_count, 2)

    def test_claim_uses_skip_locked_and_caps_twenty(self) -> None:
        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value = []

        @contextmanager
        def transaction():
            yield conn

        with patch("bot.job_worker.db.transaction", transaction):
            self.assertEqual(claim_jobs(100, "worker-a"), [])
        sql = cursor.execute.call_args.args[0]
        self.assertIn("FOR UPDATE SKIP LOCKED", sql)
        self.assertEqual(cursor.execute.call_args.args[1], (20,))

    def test_worker_groups_only_ten_sheets_jobs(self) -> None:
        claimed = [
            {
                "id": index,
                "kind": "sheets_sync_video",
                "payload": {"video_id": index},
                "attempts": 1,
                "max_attempts": 8,
            }
            for index in range(1, 13)
        ]
        settings = SimpleNamespace(job_worker_batch_size=20, job_worker_time_budget_seconds=20)

        def batch_side_effect(batch, context):
            context.sheets_video_syncs += len(batch)
            return {}

        with (
            patch("bot.job_worker.get_settings", return_value=settings),
            patch("bot.job_worker.recover_stale_jobs", return_value={"recovered": 0, "dead": 0}),
            patch("bot.job_worker.claim_jobs", return_value=claimed),
            patch("bot.job_worker._handle_sheets_video_batch", side_effect=batch_side_effect) as batch,
            patch("bot.job_worker._finish_job"),
            patch("bot.job_worker._release_unprocessed") as release,
            patch("bot.job_worker.db.fetch_one", return_value={"count": 2}),
        ):
            result = process_jobs()
        self.assertEqual(len(batch.call_args.args[0]), 10)
        self.assertEqual(len(release.call_args.args[0]), 2)
        self.assertEqual(result["done"], 10)
        self.assertEqual(result["remaining_ready"], 2)

    def test_sheet_batch_uses_single_get_clear_and_update(self) -> None:
        service = MagicMock()
        values = service.spreadsheets.return_value.values.return_value
        target = project_sheet_title("bolshe")
        wrong = next(title for title in PROJECT_SHEET_TITLES.values() if title != target)
        sheet_names = [SHEET_NAME, *PROJECT_SHEET_TITLES.values()]
        value_ranges = []
        for title in sheet_names:
            if title == SHEET_NAME or title == wrong:
                value_ranges.append({"values": [["1"]]})
            else:
                value_ranges.append({"values": []})
        values.batchGet.return_value.execute.return_value = {"valueRanges": value_ranges}
        with (
            patch("bot.sheets.get_settings", return_value=SimpleNamespace(google_sheets_spreadsheet_id="sheet")),
            patch("bot.sheets._ensure_video_sheet_columns", return_value=SHEET_COLUMNS),
            patch("bot.sheets._ensure_named_sheets"),
        ):
            result = batch_upsert_videos(
                [
                    {"id": 1, "project_code": "bolshe", "project_name": "Больше"},
                    {"id": 2, "project_code": "bolshe", "project_name": "Больше"},
                ],
                service=service,
            )
        self.assertEqual(result, {1: 2, 2: 3})
        values.batchGet.assert_called_once()
        values.batchClear.assert_called_once()
        values.batchUpdate.assert_called_once()

    def test_telegram_429_exposes_retry_after(self) -> None:
        response = MagicMock(status_code=429)
        response.json.return_value = {
            "ok": False,
            "description": "Too Many Requests",
            "parameters": {"retry_after": 37},
        }
        settings = SimpleNamespace(bot_token="token")
        with (
            patch("bot.telegram.get_settings", return_value=settings),
            patch("bot.telegram.requests.post", return_value=response),
        ):
            client = TelegramClient()
            with self.assertRaises(TelegramAPIError) as caught:
                client.send_message(1, "hello")
        self.assertEqual(caught.exception.status_code, 429)
        self.assertEqual(caught.exception.retry_after, 37)

    def test_sheets_outage_does_not_escape_approval_side_effect(self) -> None:
        actor = Actor(tg_id=1, chat_id=2, username="admin")
        with (
            patch("bot.handlers.db.execute") as update_status,
            patch("bot.handlers.jobs.enqueue_sheet_sync", side_effect=RuntimeError("Sheets unavailable")),
            patch("bot.handlers.record_system_log") as log,
        ):
            result = sync_video_after_approval({"id": 42, "updated_at": datetime.now(timezone.utc)}, actor)
        self.assertFalse(result)
        self.assertIn("sheet_sync_status = 'queued'", update_status.call_args.args[0])
        self.assertEqual(log.call_args.args[0], "sheets_sync_queue_failed")

    def test_bulk_is_blocked_when_background_jobs_are_disabled(self) -> None:
        tg = FakeTelegram()
        actor = Actor(tg_id=1, chat_id=2)
        with patch("bot.handlers.jobs.background_jobs_enabled", return_value=False):
            result = bulk_return_missing_dates(tg, actor)
        self.assertIsNone(result["operation_id"])
        self.assertIn("Фоновые задания временно отключены", tg.messages[0][1])

    def test_safe_dashboard_refresh_delegates_to_live_path(self) -> None:
        with patch(
            "bot.handlers.refresh_dashboard_live_or_enqueue",
            return_value={"queued": True, "job_id": 9},
        ) as refresh:
            result = _safe_refresh_admin_dashboard(FakeTelegram(), Actor(tg_id=1, chat_id=1))
        refresh.assert_called_once()
        self.assertEqual(result, {"queued": True, "job_id": 9})

    def test_bulk_chunk_limit_is_ten(self) -> None:
        self.assertEqual(BULK_CHUNK_SIZE, 10)


if __name__ == "__main__":
    unittest.main()
