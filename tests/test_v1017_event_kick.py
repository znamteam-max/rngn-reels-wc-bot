from __future__ import annotations

import importlib.util
import os
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import requests

from bot import jobs, worker_kick
from bot.config import DEFAULT_PUBLIC_BASE_URL, get_settings
from bot.job_worker import process_jobs
from api.webhook import _kick_worker_safely


ROOT = Path(__file__).resolve().parents[1]


class EventKickV1017Tests(unittest.TestCase):
    def tearDown(self) -> None:
        get_settings.cache_clear()

    def _settings(self, **overrides):
        values = {
            "background_jobs_enabled": True,
            "cron_secret": "secret",
            "public_base_url": "https://project-dcd2y.vercel.app",
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def test_public_base_url_has_safe_production_default(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            get_settings.cache_clear()
            self.assertEqual(get_settings().public_base_url, DEFAULT_PUBLIC_BASE_URL)

    def test_first_ready_request_is_accepted(self) -> None:
        response = MagicMock(status_code=202)
        with (
            patch("bot.worker_kick.get_settings", return_value=self._settings()),
            patch(
                "bot.worker_kick._claim_kick_lease",
                return_value={"claimed": True, "ready_jobs": 1},
            ),
            patch("bot.worker_kick.requests.post", return_value=response) as post,
            patch("bot.worker_kick._mark_kick_accepted") as accepted,
        ):
            result = worker_kick.kick_worker_if_ready(reason="approval")
        self.assertTrue(result["kicked"])
        accepted.assert_called_once_with()
        self.assertEqual(post.call_args.kwargs["timeout"], (0.5, 1.5))
        self.assertFalse(post.call_args.kwargs["allow_redirects"])
        self.assertNotIn("secret", str(result))

    def test_busy_lease_attempt_is_counted(self) -> None:
        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.side_effect = [{"count": 3}, None]

        @contextmanager
        def transaction():
            yield conn

        with patch("bot.worker_kick.db.transaction", transaction):
            result = worker_kick._claim_kick_lease(force=False)
        self.assertEqual(
            result, {"claimed": False, "ready_jobs": 3, "reason": "lease_active"}
        )
        sql = "\n".join(call.args[0] for call in cursor.execute.call_args_list)
        self.assertIn("request_count = request_count + 1", sql)
        self.assertIn("skipped_lease_count = skipped_lease_count + 1", sql)

    def test_fifty_concurrent_attempts_coalesce_to_one_post(self) -> None:
        lock = threading.Lock()
        claimed = False

        def claim(*, force: bool):
            nonlocal claimed
            with lock:
                if not claimed:
                    claimed = True
                    return {"claimed": True, "ready_jobs": 20}
                return {"claimed": False, "ready_jobs": 20, "reason": "lease_active"}

        with (
            patch("bot.worker_kick.get_settings", return_value=self._settings()),
            patch("bot.worker_kick._claim_kick_lease", side_effect=claim),
            patch(
                "bot.worker_kick.requests.post",
                return_value=MagicMock(status_code=202),
            ) as post,
            patch("bot.worker_kick._mark_kick_accepted"),
        ):
            with ThreadPoolExecutor(max_workers=20) as executor:
                results = list(
                    executor.map(
                        lambda index: worker_kick.kick_worker_if_ready(
                            reason=f"burst:{index}"
                        ),
                        range(50),
                    )
                )
        self.assertEqual(sum(bool(result["kicked"]) for result in results), 1)
        post.assert_called_once()

    def test_failed_post_expires_lease_without_raising(self) -> None:
        with (
            patch("bot.worker_kick.get_settings", return_value=self._settings()),
            patch(
                "bot.worker_kick._claim_kick_lease",
                return_value={"claimed": True, "ready_jobs": 1},
            ),
            patch(
                "bot.worker_kick.requests.post",
                side_effect=requests.ReadTimeout("private endpoint detail"),
            ),
            patch("bot.worker_kick._mark_kick_failed") as failed,
        ):
            result = worker_kick.kick_worker_if_ready(reason="webhook_tail")
        self.assertFalse(result["kicked"])
        self.assertEqual(result["error"], "kick request timed out")
        failed.assert_called_once_with("kick request timed out")

    def test_owned_enqueue_kicks_only_after_transaction_commit(self) -> None:
        committed = False
        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = {"id": 41, "inserted": False}

        @contextmanager
        def transaction():
            nonlocal committed
            yield conn
            committed = True

        def assert_committed(**_kwargs):
            self.assertTrue(committed)
            return {"kicked": True}

        with (
            patch("bot.jobs.db.transaction", transaction),
            patch(
                "bot.worker_kick.kick_worker_if_ready",
                side_effect=assert_committed,
            ) as kick,
        ):
            self.assertEqual(jobs.enqueue_dashboard_refresh(), 41)
        kick.assert_called_once_with(reason="enqueue:dashboard_refresh")

    def test_completion_clears_lease_and_counts_ready_jobs(self) -> None:
        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = {"count": 2}

        @contextmanager
        def transaction():
            yield conn

        with patch("bot.worker_kick.db.transaction", transaction):
            result = worker_kick.complete_worker_kick()
        self.assertEqual(result, {"ok": True, "ready_jobs": 2})
        sql = "\n".join(call.args[0] for call in cursor.execute.call_args_list)
        self.assertIn("lease_until = NULL", sql)
        self.assertIn("available_at <= now()", sql)

    def test_completion_endpoint_requires_post_and_cron_secret(self) -> None:
        spec = importlib.util.spec_from_file_location(
            "complete_worker_kick_endpoint",
            ROOT / "api" / "internal" / "complete-worker-kick.py",
        )
        self.assertIsNotNone(spec and spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        request = object.__new__(module.handler)
        request.command = "POST"
        request.headers = {"Authorization": "Bearer wrong", "Content-Length": "0"}
        request._send_json = MagicMock()
        with patch.object(
            module,
            "get_settings",
            return_value=SimpleNamespace(cron_secret="secret"),
        ):
            request.do_POST()
        request._send_json.assert_called_once_with(
            401, {"ok": False, "error": "unauthorized"}
        )

        request._send_json.reset_mock()
        request.headers["Authorization"] = "Bearer secret"
        with (
            patch.object(
                module,
                "get_settings",
                return_value=SimpleNamespace(cron_secret="secret"),
            ),
            patch.object(
                module,
                "complete_worker_kick",
                return_value={"ok": True, "ready_jobs": 0},
            ),
        ):
            request.do_POST()
        request._send_json.assert_called_once_with(200, {"ok": True, "ready_jobs": 0})

        request._send_json.reset_mock()
        request.do_GET()
        request._send_json.assert_called_once_with(
            405, {"ok": False, "error": "method not allowed"}
        )

    def test_event_source_is_trusted_only_after_cron_auth(self) -> None:
        spec = importlib.util.spec_from_file_location(
            "process_jobs_event_endpoint",
            ROOT / "api" / "cron" / "process-jobs.py",
        )
        self.assertIsNotNone(spec and spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        request = object.__new__(module.handler)
        request.headers = {
            "Authorization": "Bearer secret",
            "User-Agent": "rngn-event-kick/1.0",
            "X-Worker-Source": "event_kick",
        }
        with patch.object(
            module,
            "get_settings",
            return_value=SimpleNamespace(cron_secret="secret"),
        ):
            self.assertEqual(request._authenticate(), "event_kick")
        request.headers["Authorization"] = "Bearer wrong"
        with patch.object(
            module,
            "get_settings",
            return_value=SimpleNamespace(cron_secret="secret"),
        ):
            self.assertIsNone(request._authenticate())

    def test_worker_response_reports_ready_future_and_delay(self) -> None:
        settings = SimpleNamespace(
            job_worker_batch_size=20,
            job_worker_time_budget_seconds=20,
        )
        queue = {
            "ready": 0,
            "queued_total": 2,
            "next_available_in_seconds": 28,
        }
        with (
            patch("bot.job_worker.get_settings", return_value=settings),
            patch("bot.job_worker._heartbeat_started"),
            patch("bot.job_worker._heartbeat_finished"),
            patch(
                "bot.job_worker.recover_stale_jobs",
                return_value={"recovered": 0, "dead": 0},
            ),
            patch("bot.job_worker.claim_jobs", return_value=[]),
            patch("bot.job_worker.db.fetch_one", return_value=queue),
        ):
            result = process_jobs(source="event_kick")
        self.assertEqual(result["remaining_ready"], 0)
        self.assertEqual(result["remaining_queued_total"], 2)
        self.assertEqual(result["next_available_in_seconds"], 28)

    def test_future_jobs_alone_are_healthy(self) -> None:
        with patch("bot.jobs.db.fetch_one", return_value={}):
            result = jobs.worker_health_snapshot(
                {"queued": 3, "ready": 0, "future": 3},
                {"seconds_since_last_accepted": None},
            )
        self.assertTrue(result["healthy"])
        self.assertEqual(result["state"], "idle")

    def test_stale_ready_jobs_without_recent_worker_are_unhealthy(self) -> None:
        heartbeat = {
            "last_success_at": datetime.now(timezone.utc) - timedelta(minutes=10),
            "source": "event_kick",
        }
        with patch("bot.jobs.db.fetch_one", return_value=heartbeat):
            result = jobs.worker_health_snapshot(
                {"queued": 2, "ready": 2, "oldest_ready_age_seconds": 180},
                {"seconds_since_last_accepted": 180},
            )
        self.assertFalse(result["healthy"])
        self.assertEqual(result["state"], "stalled")

    def test_twenty_sheet_jobs_remain_unique_inside_business_transaction(self) -> None:
        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.side_effect = [
            {"id": index, "inserted": True} for index in range(1, 21)
        ]
        with (
            patch("bot.jobs.db.log_event"),
            patch("bot.worker_kick.kick_worker_if_ready") as kick,
        ):
            for video_id in range(1, 21):
                jobs.enqueue_sheet_sync(video_id, conn=conn)
        params = [call.args[1] for call in cursor.execute.call_args_list]
        self.assertEqual(
            {item[1] for item in params},
            {f"sheets:video:{video_id}" for video_id in range(1, 21)},
        )
        kick.assert_not_called()

    def test_hundred_mocked_webhook_tails_stay_fast_and_ignore_kick_failure(self) -> None:
        timings = []
        with patch(
            "api.webhook.kick_worker_if_ready",
            side_effect=RuntimeError("temporary kicker failure"),
        ):
            for _ in range(100):
                started = time.perf_counter()
                _kick_worker_safely("webhook_tail")
                timings.append((time.perf_counter() - started) * 1000)
        p95 = sorted(timings)[94]
        self.assertLess(p95, 3_000)


if __name__ == "__main__":
    unittest.main()
