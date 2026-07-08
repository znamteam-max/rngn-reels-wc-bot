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
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT is_nullable, column_default
                FROM information_schema.columns
                WHERE table_name = 'videos' AND column_name = 'video_type'
                """
            )
            video_type_column = cur.fetchone()
            cur.execute(
                """
                SELECT is_nullable
                FROM information_schema.columns
                WHERE table_name = 'videos' AND column_name = 'youtube_id'
                """
            )
            youtube_id_column = cur.fetchone()
            cur.execute("SELECT to_regclass('idx_videos_video_type') IS NOT NULL")
            video_type_index_exists = bool(cur.fetchone()[0])
            cur.execute("SELECT to_regclass('idx_videos_youtube_id') IS NOT NULL")
            youtube_index_exists = bool(cur.fetchone()[0])
            cur.execute(
                """
                SELECT count(*)
                FROM people
                WHERE role = 'author'
                  AND lower(name) = lower('Прокудин')
                  AND username = 'ny_pochemu'
                  AND is_active = true
                """
            )
            prokudin_active_rows = int(cur.fetchone()[0])
        conn.commit()

    _LAST_RESULT = {
        "applied": True,
        "schema": {
            "video_type_column": video_type_column is not None,
            "video_type_nullable": video_type_column[0] if video_type_column else None,
            "video_type_default": video_type_column[1] if video_type_column else None,
            "youtube_id_column": youtube_id_column is not None,
            "youtube_id_nullable": youtube_id_column[0] if youtube_id_column else None,
            "idx_videos_video_type": video_type_index_exists,
            "idx_videos_youtube_id": youtube_index_exists,
        },
        "seed": {
            "prokudin_action": seed_action,
            "prokudin_id": person_id,
            "prokudin_active_rows": prokudin_active_rows,
        },
    }
    _DONE = True
    return _LAST_RESULT
