from __future__ import annotations

from typing import Any

import psycopg

from bot.config import get_settings
from bot.people_seeds import seed_and_backfill_egor
from bot.projects import seed_projects
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
        egor_montage = seed_and_backfill_egor(conn)
        active_project_count = seed_projects(conn)
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
            cur.execute("SELECT to_regclass('admin_queue_state') IS NOT NULL")
            admin_queue_state_exists = bool(cur.fetchone()[0])
            cur.execute("SELECT to_regclass('idx_videos_pending_fifo') IS NOT NULL")
            pending_fifo_index_exists = bool(cur.fetchone()[0])
            cur.execute("SELECT count(*) FROM admin_queue_state WHERE queue_name = 'main'")
            admin_queue_main_rows = int(cur.fetchone()[0])
            cur.execute("SELECT to_regclass('projects') IS NOT NULL")
            projects_table_exists = bool(cur.fetchone()[0])
            cur.execute("SELECT to_regclass('idx_videos_project_id') IS NOT NULL")
            project_index_exists = bool(cur.fetchone()[0])
            cur.execute("SELECT to_regclass('idx_videos_status_project') IS NOT NULL")
            status_project_index_exists = bool(cur.fetchone()[0])
            cur.execute(
                """
                SELECT count(*)
                FROM information_schema.columns
                WHERE table_name = 'videos'
                  AND column_name IN ('project_id', 'project_code', 'project_name')
                """
            )
            video_project_columns = int(cur.fetchone()[0])
            cur.execute(
                """
                SELECT count(*)
                FROM information_schema.columns
                WHERE table_name = 'admin_queue_state'
                  AND column_name IN ('dashboard_chat_id', 'dashboard_message_id', 'dashboard_updated_at')
                """
            )
            dashboard_columns = int(cur.fetchone()[0])
            cur.execute(
                """
                SELECT count(*)
                FROM information_schema.columns
                WHERE table_name = 'admin_queue_state'
                  AND column_name IN ('queue_filter_type', 'queue_filter_value')
                """
            )
            queue_filter_columns = int(cur.fetchone()[0])
            cur.execute("SELECT to_regclass('daily_reports') IS NOT NULL")
            daily_reports_table_exists = bool(cur.fetchone()[0])
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
            "admin_queue_state": admin_queue_state_exists,
            "idx_videos_pending_fifo": pending_fifo_index_exists,
            "admin_queue_main_rows": admin_queue_main_rows,
            "projects_table": projects_table_exists,
            "video_project_columns": video_project_columns,
            "idx_videos_project_id": project_index_exists,
            "idx_videos_status_project": status_project_index_exists,
            "admin_dashboard_columns": dashboard_columns,
            "admin_queue_filter_columns": queue_filter_columns,
            "daily_reports_table": daily_reports_table_exists,
        },
        "seed": {
            "prokudin_action": seed_action,
            "prokudin_id": person_id,
            "prokudin_active_rows": prokudin_active_rows,
            "active_project_count": active_project_count,
            "egor_montage_action": egor_montage["action"],
            "egor_montage_id": egor_montage["person_id"],
            "egor_montage_active_rows": egor_montage["active_rows"],
            "egor_montage_backfilled_count": egor_montage["backfilled_count"],
        },
    }
    _DONE = True
    return _LAST_RESULT
