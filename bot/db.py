from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from bot.config import get_settings


def connect() -> psycopg.Connection:
    settings = get_settings()
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL is not configured")
    return psycopg.connect(settings.database_url, row_factory=dict_row)


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

