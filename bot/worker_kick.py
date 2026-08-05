from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

import psycopg
import requests

from bot import db
from bot.config import get_settings


KICK_WORKER_NAME = "main"
KICK_LEASE_SECONDS = 8
KICK_CONNECT_TIMEOUT_SECONDS = 0.5
KICK_READ_TIMEOUT_SECONDS = 1.5


def _safe_error(exc: Exception) -> str:
    if isinstance(exc, requests.Timeout):
        return "kick request timed out"
    if isinstance(exc, requests.ConnectionError):
        return "kick connection failed"
    if isinstance(exc, psycopg.Error):
        return "kick database operation failed"
    text = f"{type(exc).__name__}: {exc}"
    settings = get_settings()
    for secret in (
        getattr(settings, "cron_secret", None),
        getattr(settings, "database_url", None),
        getattr(settings, "public_base_url", None),
    ):
        if secret:
            text = text.replace(secret, "[secret]")
    return text[:200]


def _claim_kick_lease(*, force: bool) -> dict[str, Any]:
    with db.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO worker_kick_state (worker_name)
                VALUES (%s)
                ON CONFLICT (worker_name) DO NOTHING
                """,
                (KICK_WORKER_NAME,),
            )
            cur.execute(
                """
                UPDATE worker_kick_state
                SET last_requested_at = now(),
                    request_count = request_count + 1,
                    updated_at = now()
                WHERE worker_name = %s
                """,
                (KICK_WORKER_NAME,),
            )
            cur.execute(
                """
                SELECT count(*) AS count
                FROM background_jobs
                WHERE status = 'queued'
                  AND available_at <= now()
                """
            )
            ready_jobs = int((cur.fetchone() or {}).get("count") or 0)
            if ready_jobs <= 0:
                return {"claimed": False, "ready_jobs": 0, "reason": "no_ready_jobs"}
            cur.execute(
                """
                UPDATE worker_kick_state
                SET lease_until = now() + make_interval(secs => %s),
                    updated_at = now()
                WHERE worker_name = %s
                  AND (%s OR lease_until IS NULL OR lease_until <= now())
                RETURNING lease_until
                """,
                (KICK_LEASE_SECONDS, KICK_WORKER_NAME, bool(force)),
            )
            claimed = cur.fetchone()
            if claimed:
                return {"claimed": True, "ready_jobs": ready_jobs}
            cur.execute(
                """
                UPDATE worker_kick_state
                SET skipped_lease_count = skipped_lease_count + 1,
                    updated_at = now()
                WHERE worker_name = %s
                """,
                (KICK_WORKER_NAME,),
            )
            return {"claimed": False, "ready_jobs": ready_jobs, "reason": "lease_active"}


def _mark_kick_accepted() -> None:
    db.execute(
        """
        UPDATE worker_kick_state
        SET last_accepted_at = now(),
            accepted_count = accepted_count + 1,
            last_error = NULL,
            updated_at = now()
        WHERE worker_name = %s
        """,
        (KICK_WORKER_NAME,),
    )


def _mark_kick_failed(error: str) -> None:
    db.execute(
        """
        UPDATE worker_kick_state
        SET lease_until = NULL,
            last_error_at = now(),
            last_error = %s,
            updated_at = now()
        WHERE worker_name = %s
        """,
        (error[:200], KICK_WORKER_NAME),
    )


def _kicker_url(public_base_url: str) -> str:
    base = public_base_url.strip().rstrip("/")
    parsed = urlsplit(base)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("PUBLIC_BASE_URL is invalid")
    return f"{base}/api/internal/kick-worker"


def kick_worker_if_ready(*, reason: str, force: bool = False) -> dict[str, object]:
    settings = get_settings()
    if not settings.background_jobs_enabled:
        return {"kicked": False, "reason": "background_jobs_disabled"}
    try:
        lease = _claim_kick_lease(force=force)
    except Exception as exc:
        return {"kicked": False, "reason": "lease_error", "error": _safe_error(exc)}
    if not lease.get("claimed"):
        return {
            "kicked": False,
            "reason": lease.get("reason") or "not_claimed",
            "ready_jobs": int(lease.get("ready_jobs") or 0),
        }
    if not settings.cron_secret:
        error = "CRON_SECRET is not configured"
        try:
            _mark_kick_failed(error)
        except Exception:
            pass
        return {"kicked": False, "reason": "configuration_error", "error": error}
    try:
        url = _kicker_url(settings.public_base_url)
        response = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {settings.cron_secret}",
                "User-Agent": "rngn-worker-kick-client/1.0",
                "Content-Length": "0",
            },
            data=b"",
            timeout=(KICK_CONNECT_TIMEOUT_SECONDS, KICK_READ_TIMEOUT_SECONDS),
            allow_redirects=False,
        )
        if response.status_code != 202:
            raise RuntimeError(f"kick endpoint returned HTTP {response.status_code}")
    except Exception as exc:
        error = _safe_error(exc)
        try:
            _mark_kick_failed(error)
        except Exception:
            pass
        return {"kicked": False, "reason": "request_failed", "error": error}
    result: dict[str, object] = {
        "kicked": True,
        "accepted": True,
        "reason": reason[:80],
        "ready_jobs": int(lease.get("ready_jobs") or 0),
    }
    try:
        _mark_kick_accepted()
    except Exception as exc:
        result["state_warning"] = _safe_error(exc)
    return result


def complete_worker_kick() -> dict[str, object]:
    with db.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO worker_kick_state (worker_name)
                VALUES (%s)
                ON CONFLICT (worker_name) DO NOTHING
                """,
                (KICK_WORKER_NAME,),
            )
            cur.execute(
                """
                UPDATE worker_kick_state
                SET lease_until = NULL,
                    last_completed_at = now(),
                    updated_at = now()
                WHERE worker_name = %s
                """,
                (KICK_WORKER_NAME,),
            )
            cur.execute(
                """
                SELECT count(*) AS count
                FROM background_jobs
                WHERE status = 'queued'
                  AND available_at <= now()
                """
            )
            ready_jobs = int((cur.fetchone() or {}).get("count") or 0)
    return {"ok": True, "ready_jobs": ready_jobs}


def worker_kick_snapshot() -> dict[str, object]:
    try:
        row = db.fetch_one(
            """
            SELECT
                lease_until,
                lease_until > now() AS lease_active,
                last_requested_at,
                last_accepted_at,
                last_completed_at,
                last_error_at,
                last_error,
                request_count,
                accepted_count,
                skipped_lease_count
            FROM worker_kick_state
            WHERE worker_name = %s
            """,
            (KICK_WORKER_NAME,),
        ) or {}
    except psycopg.Error:
        row = {}
    last_accepted_at = row.get("last_accepted_at")
    seconds_since_last_accepted: int | None = None
    if last_accepted_at:
        if last_accepted_at.tzinfo is None:
            last_accepted_at = last_accepted_at.replace(tzinfo=timezone.utc)
        seconds_since_last_accepted = max(
            0,
            int((datetime.now(timezone.utc) - last_accepted_at).total_seconds()),
        )
    return {
        "lease_active": bool(row.get("lease_active")),
        "last_requested_at": row["last_requested_at"].isoformat()
        if row.get("last_requested_at")
        else None,
        "last_accepted_at": last_accepted_at.isoformat() if last_accepted_at else None,
        "last_completed_at": row["last_completed_at"].isoformat()
        if row.get("last_completed_at")
        else None,
        "seconds_since_last_accepted": seconds_since_last_accepted,
        "request_count": int(row.get("request_count") or 0),
        "accepted_count": int(row.get("accepted_count") or 0),
        "skipped_lease_count": int(row.get("skipped_lease_count") or 0),
        "last_error_at": row["last_error_at"].isoformat() if row.get("last_error_at") else None,
        "last_error": row.get("last_error"),
    }
