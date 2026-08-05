from __future__ import annotations

import importlib.util
import time
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from bot import jobs
from bot.github_oidc import GitHubOIDCError, validate_github_oidc_token
from bot.handlers import (
    Actor,
    pump_queue_live_or_enqueue,
    refresh_dashboard_live_or_enqueue,
)
from bot.job_worker import _heartbeat_finished, _heartbeat_started, process_jobs

ROOT = Path(__file__).resolve().parents[1]


class _SigningKey:
    def __init__(self, key) -> None:
        self.key = key


class _JWKSClient:
    def __init__(self, key) -> None:
        self.key = key

    def get_signing_key_from_jwt(self, _token: str) -> _SigningKey:
        return _SigningKey(self.key)


class GitHubOIDCV1016Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.public_key = cls.private_key.public_key()

    def _claims(self, **overrides):
        now = int(time.time())
        claims = {
            "iss": "https://token.actions.githubusercontent.com",
            "aud": "rngn-reels-wc-worker",
            "iat": now - 1,
            "exp": now + 300,
            "repository": "znamteam-max/rngn-reels-wc-bot",
            "repository_owner": "znamteam-max",
            "ref": "refs/heads/main",
            "event_name": "schedule",
        }
        claims.update(overrides)
        return claims

    def _token(self, **overrides) -> str:
        return jwt.encode(
            self._claims(**overrides),
            self.private_key,
            algorithm="RS256",
            headers={"kid": "test-key"},
        )

    def _validate(self, token: str):
        with patch(
            "bot.github_oidc._get_jwks_client",
            return_value=_JWKSClient(self.public_key),
        ):
            return validate_github_oidc_token(token)

    def test_valid_github_oidc(self) -> None:
        claims = self._validate(self._token(event_name="workflow_dispatch"))
        self.assertEqual(claims["repository"], "znamteam-max/rngn-reels-wc-bot")

    def test_wrong_repository_is_rejected(self) -> None:
        with self.assertRaises(GitHubOIDCError):
            self._validate(self._token(repository="someone/else"))

    def test_wrong_ref_is_rejected(self) -> None:
        with self.assertRaises(GitHubOIDCError):
            self._validate(self._token(ref="refs/heads/feature"))

    def test_wrong_audience_is_rejected(self) -> None:
        with self.assertRaises(GitHubOIDCError):
            self._validate(self._token(aud="wrong-audience"))

    def test_expired_token_is_rejected(self) -> None:
        with self.assertRaises(GitHubOIDCError):
            self._validate(self._token(exp=int(time.time()) - 10))

    def test_unsigned_token_is_rejected(self) -> None:
        token = jwt.encode(
            self._claims(), key="", algorithm="none", headers={"kid": "none"}
        )
        with self.assertRaises(GitHubOIDCError):
            self._validate(token)

    def test_cron_secret_auth_still_works(self) -> None:
        spec = importlib.util.spec_from_file_location(
            "process_jobs_endpoint",
            ROOT / "api" / "cron" / "process-jobs.py",
        )
        self.assertIsNotNone(spec and spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        request = object.__new__(module.handler)
        request.headers = {
            "Authorization": "Bearer secret",
            "User-Agent": "vercel-cron/1.0",
        }
        with patch.object(
            module, "get_settings", return_value=SimpleNamespace(cron_secret="secret")
        ):
            self.assertEqual(request._authenticate(), "vercel_cron")


class _LiveCursor:
    def __init__(self, *, updated_at=None, lock=True) -> None:
        self.updated_at = updated_at
        self.lock = lock
        self.row = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, _params=()):
        if "dashboard_updated_at" in sql:
            self.row = {"dashboard_updated_at": self.updated_at}
        elif "pg_try_advisory_lock" in sql:
            self.row = {"locked": self.lock}
        else:
            self.row = {"unlocked": True}

    def fetchone(self):
        return self.row


class _LiveConnection:
    def __init__(self, cursor: _LiveCursor) -> None:
        self.live_cursor = cursor
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return self.live_cursor

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class LiveQueueV1016Tests(unittest.TestCase):
    def _connect(self, conn):
        @contextmanager
        def connect(*, timeout=None):
            self.assertEqual(timeout, 2)
            yield conn

        return connect

    def test_dashboard_debounce_enqueues_repair(self) -> None:
        conn = _LiveConnection(_LiveCursor(updated_at=datetime.now(timezone.utc)))
        with (
            patch("bot.handlers.db.connect", self._connect(conn)),
            patch(
                "bot.handlers.jobs.enqueue_dashboard_refresh", return_value=9
            ) as enqueue,
        ):
            result = refresh_dashboard_live_or_enqueue(MagicMock(), reason="burst")
        self.assertTrue(result["debounced"])
        self.assertEqual(result["job_id"], 9)
        enqueue.assert_called_once()

    def test_advisory_lock_contention_enqueues_repair(self) -> None:
        conn = _LiveConnection(_LiveCursor(lock=False))
        with (
            patch("bot.handlers.db.connect", self._connect(conn)),
            patch(
                "bot.handlers.jobs.enqueue_dashboard_refresh", return_value=10
            ) as enqueue,
        ):
            result = refresh_dashboard_live_or_enqueue(MagicMock(), reason="contention")
        self.assertTrue(result["lock_busy"])
        enqueue.assert_called_once()

    def test_first_live_event_refreshes_dashboard_once(self) -> None:
        conn = _LiveConnection(_LiveCursor())
        tg = MagicMock(timeout=15)
        with (
            patch("bot.handlers.db.connect", self._connect(conn)),
            patch(
                "bot.handlers._refresh_admin_dashboard_with_conn",
                return_value={"message_id": 234, "created": False},
            ) as refresh,
            patch("bot.handlers.jobs.enqueue_dashboard_refresh") as enqueue,
        ):
            result = refresh_dashboard_live_or_enqueue(tg, reason="first")
        self.assertEqual(result["message_id"], 234)
        refresh.assert_called_once_with(tg, conn, None)
        enqueue.assert_not_called()
        self.assertEqual(tg.timeout, 15)

    def test_dashboard_failure_enqueues_repair(self) -> None:
        with (
            patch(
                "bot.handlers.db.connect",
                side_effect=RuntimeError("database unavailable"),
            ),
            patch(
                "bot.handlers.jobs.enqueue_dashboard_refresh", return_value=11
            ) as enqueue,
            patch("bot.handlers.record_system_log"),
        ):
            result = refresh_dashboard_live_or_enqueue(MagicMock(), reason="failure")
        self.assertTrue(result["failed"])
        enqueue.assert_called_once()

    def test_queue_pump_failure_enqueues_repair(self) -> None:
        tg = MagicMock(timeout=15)
        actor = Actor(tg_id=1, chat_id=2)
        with (
            patch(
                "bot.handlers.pump_admin_queue",
                side_effect=RuntimeError("Telegram unavailable"),
            ),
            patch(
                "bot.handlers.jobs.enqueue_admin_queue_pump", return_value=12
            ) as enqueue,
            patch("bot.handlers.record_system_log"),
        ):
            result = pump_queue_live_or_enqueue(tg, actor, reason="failure")
        self.assertIsNone(result)
        enqueue.assert_called_once_with(force_repost=False)
        self.assertEqual(tg.timeout, 15)


class WorkerHeartbeatV1016Tests(unittest.TestCase):
    def test_heartbeat_updates_source_and_counts(self) -> None:
        result = {
            "claimed": 4,
            "done": 3,
            "remaining_ready": 1,
            "invocation_id": "run-1",
        }
        with patch("bot.job_worker.db.execute") as execute:
            _heartbeat_started("run-1", "github_actions")
            _heartbeat_finished(result, "github_actions")
        self.assertEqual(execute.call_count, 2)
        self.assertIn("worker_heartbeats", execute.call_args_list[0].args[0])
        self.assertEqual(
            execute.call_args_list[1].args[1], (4, 3, 1, "github_actions", "run-1")
        )

    def test_health_warns_for_stale_worker_with_queued_jobs(self) -> None:
        heartbeat = {
            "last_success_at": datetime.now(timezone.utc) - timedelta(minutes=17),
            "last_claimed": 2,
            "last_done": 2,
            "last_remaining": 0,
            "source": "github_actions",
        }
        with patch("bot.jobs.db.fetch_one", return_value=heartbeat):
            result = jobs.worker_health_snapshot({"queued": 23})
        self.assertFalse(result["healthy"])
        self.assertEqual(result["warning"], "queued jobs are not being processed")

    def test_empty_queue_does_not_report_worker_outage(self) -> None:
        with patch("bot.jobs.db.fetch_one", return_value={}):
            result = jobs.worker_health_snapshot({"queued": 0})
        self.assertTrue(result["healthy"])
        self.assertNotIn("warning", result)

    def test_worker_response_contains_processed_by_kind(self) -> None:
        claimed = [
            {
                "id": 1,
                "kind": "dashboard_refresh",
                "payload": {},
                "attempts": 1,
                "max_attempts": 8,
            }
        ]
        settings = SimpleNamespace(
            job_worker_batch_size=20, job_worker_time_budget_seconds=20
        )
        with (
            patch("bot.job_worker.get_settings", return_value=settings),
            patch("bot.job_worker._heartbeat_started"),
            patch("bot.job_worker._heartbeat_finished"),
            patch(
                "bot.job_worker.recover_stale_jobs",
                return_value={"recovered": 0, "dead": 0},
            ),
            patch("bot.job_worker.claim_jobs", return_value=claimed),
            patch("bot.job_worker.JOB_HANDLERS", {"dashboard_refresh": MagicMock()}),
            patch("bot.job_worker._finish_job"),
            patch("bot.job_worker.db.fetch_one", return_value={"count": 0}),
        ):
            result = process_jobs(source="github_actions")
        self.assertEqual(result["processed_by_kind"], {"dashboard_refresh": 1})

    def test_priority_helpers_match_worker_contract(self) -> None:
        with patch("bot.jobs.enqueue_job", return_value=1) as enqueue:
            jobs.enqueue_admin_queue_pump()
            jobs.enqueue_dashboard_refresh()
            jobs.enqueue_sheet_sync(42)
            jobs.enqueue_telegram_notification(1, "text", event_key="bulk")
        self.assertEqual(
            [call.kwargs["priority"] for call in enqueue.call_args_list],
            [5, 20, 60, 80],
        )


class WorkflowV1016Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workflow = (
            ROOT / ".github" / "workflows" / "process-background-jobs.yml"
        ).read_text(encoding="utf-8")

    def test_workflow_exits_when_remaining_is_zero(self) -> None:
        self.assertIn("remaining > 0 && calls < 12", self.workflow)
        self.assertIn("remaining_ready", self.workflow)

    def test_workflow_stops_after_twelve_calls(self) -> None:
        self.assertIn("calls < 12", self.workflow)
        self.assertIn("after 12 bounded calls", self.workflow)

    def test_workflow_masks_token_and_does_not_enable_shell_tracing(self) -> None:
        self.assertIn('echo "::add-mask::${OIDC_TOKEN}"', self.workflow)
        self.assertNotIn("set -x", self.workflow)
        self.assertNotIn('echo "${OIDC_TOKEN}"', self.workflow)


if __name__ == "__main__":
    unittest.main()
