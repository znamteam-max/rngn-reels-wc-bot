from __future__ import annotations

from contextlib import contextmanager
from threading import Lock
from typing import Any, Iterator
from urllib.parse import urlparse

import psycopg
from psycopg_pool import ConnectionPool
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from bot.config import get_settings


_POOL: ConnectionPool | None = None
_POOL_CONNINFO: str | None = None
_POOL_LOCK = Lock()


def _get_pool() -> ConnectionPool:
    global _POOL, _POOL_CONNINFO
    settings = get_settings()
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL is not configured")
    if _POOL is not None and _POOL_CONNINFO == settings.database_url:
        return _POOL
    with _POOL_LOCK:
        if _POOL is not None and _POOL_CONNINFO == settings.database_url:
            return _POOL
        if _POOL is not None:
            _POOL.close()
        _POOL = ConnectionPool(
            conninfo=settings.database_url,
            min_size=0,
            max_size=settings.db_pool_max_size,
            timeout=5,
            max_idle=60,
            kwargs={"row_factory": dict_row},
            open=True,
        )
        _POOL_CONNINFO = settings.database_url
        return _POOL


def close_pool() -> None:
    global _POOL, _POOL_CONNINFO
    with _POOL_LOCK:
        if _POOL is not None:
            _POOL.close()
        _POOL = None
        _POOL_CONNINFO = None


def pooled_endpoint_detected() -> bool:
    database_url = get_settings().database_url
    if not database_url:
        return False
    hostname = (urlparse(database_url).hostname or "").lower()
    return "-pooler." in hostname or ".pooler." in hostname or hostname.startswith("pooler.")


def pool_diagnostics() -> dict[str, Any]:
    settings = get_settings()
    return {
        "pool_enabled": bool(settings.database_url),
        "pool_max_size": settings.db_pool_max_size,
        "pooled_endpoint_detected": pooled_endpoint_detected(),
    }


@contextmanager
def connect(*, timeout: float | None = None) -> Iterator[psycopg.Connection]:
    with _get_pool().connection(timeout=timeout) as conn:
        yield conn


@contextmanager
def transaction() -> Iterator[psycopg.Connection]:
    with connect() as conn:
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def fetch_one(sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchone()


def fetch_all(sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return list(cur.fetchall())


def execute(sql: str, params: tuple[Any, ...] = ()) -> None:
    with transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)


def log_event(
    conn: psycopg.Connection,
    *,
    entity_type: str,
    entity_id: int | None,
    action: str,
    actor_tg_id: int | None = None,
    actor_username: str | None = None,
    before_data: dict[str, Any] | None = None,
    after_data: dict[str, Any] | None = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO logs (
                entity_type, entity_id, action, actor_tg_id, actor_username,
                before_data, after_data
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                entity_type,
                entity_id,
                action,
                actor_tg_id,
                actor_username,
                Jsonb(before_data) if before_data is not None else None,
                Jsonb(after_data) if after_data is not None else None,
            ),
        )


def get_session(tg_id: int) -> dict[str, Any] | None:
    return fetch_one(
        "SELECT state, data FROM user_sessions WHERE tg_id = %s",
        (tg_id,),
    )


def set_session(
    *,
    tg_id: int,
    chat_id: int,
    username: str | None,
    state: str,
    data: dict[str, Any],
) -> None:
    execute(
        """
        INSERT INTO user_sessions (tg_id, chat_id, username, state, data, updated_at)
        VALUES (%s, %s, %s, %s, %s, now())
        ON CONFLICT (tg_id)
        DO UPDATE SET
            chat_id = EXCLUDED.chat_id,
            username = EXCLUDED.username,
            state = EXCLUDED.state,
            data = EXCLUDED.data,
            updated_at = now()
        """,
        (tg_id, chat_id, username, state, Jsonb(data)),
    )


def clear_session(tg_id: int) -> None:
    execute("DELETE FROM user_sessions WHERE tg_id = %s", (tg_id,))


def current_schema_version() -> str | None:
    try:
        row = fetch_one("SELECT version FROM schema_versions ORDER BY applied_at DESC, version DESC LIMIT 1")
    except psycopg.Error:
        return None
    return str(row["version"]) if row and row.get("version") else None
