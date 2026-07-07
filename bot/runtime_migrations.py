from __future__ import annotations

from typing import Any

import psycopg

from bot.config import get_settings
from scripts.init_db import SCHEMA_SQL
from scripts.seed_people import upsert_person


_DONE = False
_LAST_RESULT: dict[str, Any] = {"applied": False}


def ensure_runtime_migrations() -> dict[str, Any]:
    global _DONE, _LAST_RESULT
    if _DONE:
        return _LAST_RESULT

    settings = get_settings()
    if not settings.database_url:
        _LAST_RESULT = {"applied": False, "skipped": "DATABASE_URL is not configured"}
        return _LAST_RESULT

    with psycopg.connect(settings.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(SCHEMA_SQL)
        seed_action, person_id = upsert_person(
            conn,
            {
                "role": "author",
                "name": "Прокудин",
                "username": "ny_pochemu",
                "sort_weight": "15",
                "is_active": "true",
            },
        )
        conn.commit()

    _LAST_RESULT = {
        "applied": True,
        "prokudin_action": seed_action,
        "prokudin_id": person_id,
    }
    _DONE = True
    return _LAST_RESULT
