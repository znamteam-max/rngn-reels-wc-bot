from __future__ import annotations

import uuid
from typing import Any

import psycopg

from bot.config import get_settings
from bot.people_seeds import seed_and_backfill_egor
from bot.projects import seed_projects
from scripts.init_db import SCHEMA_SQL
from scripts.seed_people import upsert_person


_DONE = False
_LAST_RESULT: dict[str, Any] = {"applied": False}


def _fetchone_dict(cursor) -> dict[str, Any]:
    row = cursor.fetchone()
    if row is None:
        return {}
    return {
        getattr(column, "name", str(column)): value
        for column, value in zip(cursor.description or (), row)
    }


def ensure_runtime_migrations(*, force: bool = False) -> dict[str, Any]:
    global _DONE, _LAST_RESULT
    if _DONE and not force:
        return _LAST_RESULT

    settings = get_settings()
    if not settings.database_url:
        _LAST_RESULT = {"applied": False, "skipped": "DATABASE_URL is not configured"}
        return _LAST_RESULT

    with psycopg.connect(settings.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('schema_versions') IS NOT NULL")
            versions_table_before = bool(cur.fetchone()[0])
            version_already_applied = False
            if versions_table_before:
                cur.execute("SELECT EXISTS(SELECT 1 FROM schema_versions WHERE version = '1.0.18')")
                version_already_applied = bool(cur.fetchone()[0])
            pre_queue_snapshot: dict[str, Any] = {}
            if versions_table_before:
                cur.execute(
                    """
                    SELECT
                        q.active_video_id,
                        q.active_chat_id,
                        q.active_message_id,
                        v.status AS active_status,
                        (SELECT count(*) FROM videos WHERE status = 'pending') AS pending_count,
                        (
                            SELECT count(*) FROM videos
                            WHERE status = 'pending' AND admin_message_id IS NOT NULL
                        ) AS pending_with_message_id,
                        (
                            SELECT count(*) FROM videos
                            WHERE status = 'pending'
                              AND admin_message_id IS NOT NULL
                              AND (q.active_video_id IS NULL OR id <> q.active_video_id)
                        ) AS non_active_pending_with_message_id
                    FROM admin_queue_state q
                    LEFT JOIN videos v ON v.id = q.active_video_id
                    WHERE q.queue_name = 'main'
                    """
                )
                pre_queue_snapshot = _fetchone_dict(cur)
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
            cur.execute("SELECT to_regclass('telegram_updates') IS NOT NULL")
            telegram_updates_table_exists = bool(cur.fetchone()[0])
            cur.execute("SELECT to_regclass('background_jobs') IS NOT NULL")
            background_jobs_table_exists = bool(cur.fetchone()[0])
            cur.execute("SELECT to_regclass('bulk_operations') IS NOT NULL")
            bulk_operations_table_exists = bool(cur.fetchone()[0])
            cur.execute("SELECT to_regclass('worker_heartbeats') IS NOT NULL")
            worker_heartbeats_table_exists = bool(cur.fetchone()[0])
            cur.execute("SELECT to_regclass('worker_kick_state') IS NOT NULL")
            worker_kick_state_table_exists = bool(cur.fetchone()[0])
            cur.execute("SELECT to_regclass('schema_versions') IS NOT NULL")
            schema_versions_table_exists = bool(cur.fetchone()[0])
            cur.execute("SELECT EXISTS(SELECT 1 FROM schema_versions WHERE version = '1.0.18')")
            schema_version_applied = bool(cur.fetchone()[0])
            cur.execute(
                """
                SELECT count(*)
                FROM information_schema.columns
                WHERE table_name = 'admin_queue_state'
                  AND column_name IN (
                    'active_reservation_token', 'active_reserved_at',
                    'active_generation', 'active_delivery_attempts',
                    'active_last_error', 'active_last_error_at',
                    'last_repaired_at', 'last_repair_reason'
                  )
                """
            )
            atomic_queue_columns = int(cur.fetchone()[0])
            cur.execute(
                """
                SELECT count(*)
                FROM information_schema.columns
                WHERE table_name = 'background_jobs'
                  AND column_name IN (
                    'first_error', 'first_failed_at',
                    'last_failed_at', 'failure_count'
                  )
                """
            )
            job_failure_columns = int(cur.fetchone()[0])
            cur.execute(
                """
                SELECT count(*)
                FROM information_schema.columns
                WHERE table_name = 'videos'
                  AND column_name IN (
                    'sheet_sync_status', 'sheet_sync_attempts',
                    'sheet_sync_error', 'sheet_synced_at'
                  )
                """
            )
            sheet_sync_columns = int(cur.fetchone()[0])
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
            stale_metadata_cleared = 0
            repair_action = "already_applied"
            if not version_already_applied:
                cur.execute(
                    "SELECT * FROM admin_queue_state WHERE queue_name = 'main' FOR UPDATE"
                )
                queue_state = _fetchone_dict(cur)
                active_video_id = queue_state.get("active_video_id")
                active_video = None
                if active_video_id:
                    cur.execute(
                        "SELECT id, status FROM videos WHERE id = %s FOR UPDATE",
                        (active_video_id,),
                    )
                    active_video = _fetchone_dict(cur)
                if active_video and active_video.get("status") == "pending":
                    has_message = bool(queue_state.get("active_message_id"))
                    cur.execute(
                        """
                        UPDATE admin_queue_state
                        SET active_chat_id = %s,
                            active_reservation_token = %s,
                            active_reserved_at = CASE
                                WHEN %s THEN now()
                                ELSE now() - interval '6 seconds'
                            END,
                            active_generation = GREATEST(active_generation, 1),
                            active_delivery_attempts = GREATEST(active_delivery_attempts, 1),
                            active_last_error = NULL,
                            active_last_error_at = NULL,
                            last_repaired_at = now(),
                            last_repair_reason = 'v1.0.18 migration preserved active pending pointer',
                            updated_at = now()
                        WHERE queue_name = 'main'
                        """,
                        (settings.admin_chat_id, uuid.uuid4(), has_message),
                    )
                    if has_message:
                        cur.execute(
                            """
                            UPDATE videos
                            SET admin_message_chat_id = %s,
                                admin_message_id = %s,
                                admin_notified_at = COALESCE(admin_notified_at, now()),
                                updated_at = now()
                            WHERE id = %s
                            """,
                            (
                                settings.admin_chat_id,
                                queue_state.get("active_message_id"),
                                active_video_id,
                            ),
                        )
                    else:
                        cur.execute(
                            """
                            UPDATE videos
                            SET admin_message_chat_id = NULL,
                                admin_message_id = NULL,
                                admin_notified_at = NULL,
                                updated_at = now()
                            WHERE id = %s
                            """,
                            (active_video_id,),
                        )
                    repair_action = "active_pending_preserved"
                else:
                    cur.execute(
                        """
                        UPDATE admin_queue_state
                        SET active_video_id = NULL,
                            active_chat_id = NULL,
                            active_message_id = NULL,
                            active_reservation_token = NULL,
                            active_reserved_at = NULL,
                            claimed_by_tg_id = NULL,
                            claimed_by_username = NULL,
                            claimed_at = NULL,
                            last_repaired_at = now(),
                            last_repair_reason = 'v1.0.18 migration cleared invalid active pointer',
                            updated_at = now()
                        WHERE queue_name = 'main'
                        """
                    )
                    active_video_id = None
                    repair_action = "invalid_pointer_cleared"
                cur.execute(
                    """
                    UPDATE videos
                    SET admin_message_chat_id = NULL,
                        admin_message_id = NULL,
                        admin_notified_at = NULL,
                        updated_at = now()
                    WHERE status = 'pending'
                      AND (%s IS NULL OR id <> %s)
                      AND admin_message_id IS NOT NULL
                    """,
                    (active_video_id, active_video_id),
                )
                stale_metadata_cleared = int(cur.rowcount or 0)
                cur.execute(
                    """
                    INSERT INTO background_jobs (kind, dedupe_key, payload, priority)
                    VALUES ('admin_queue_pump', 'queue:pump:main', '{}'::jsonb, 5)
                    ON CONFLICT (dedupe_key)
                    WHERE dedupe_key IS NOT NULL
                      AND status IN ('queued', 'processing')
                    DO UPDATE SET
                        priority = LEAST(background_jobs.priority, EXCLUDED.priority),
                        available_at = LEAST(background_jobs.available_at, now()),
                        updated_at = now()
                    """
                )
                cur.execute(
                    """
                    INSERT INTO background_jobs (kind, dedupe_key, payload, priority)
                    VALUES ('dashboard_refresh', 'dashboard:main', '{}'::jsonb, 20)
                    ON CONFLICT (dedupe_key)
                    WHERE dedupe_key IS NOT NULL
                      AND status IN ('queued', 'processing')
                    DO NOTHING
                    """
                )
            cur.execute(
                """
                SELECT
                    q.active_video_id,
                    q.active_chat_id,
                    q.active_message_id,
                    q.active_generation,
                    q.active_delivery_attempts,
                    (q.active_reservation_token IS NOT NULL) AS has_reservation_token,
                    v.status AS active_status,
                    (SELECT count(*) FROM videos WHERE status = 'pending') AS pending_count,
                    (
                        SELECT count(*) FROM videos
                        WHERE status = 'pending' AND admin_message_id IS NOT NULL
                    ) AS pending_with_message_id,
                    (
                        SELECT count(*) FROM videos
                        WHERE status = 'pending'
                          AND admin_message_id IS NOT NULL
                          AND (q.active_video_id IS NULL OR id <> q.active_video_id)
                    ) AS non_active_pending_with_message_id
                FROM admin_queue_state q
                LEFT JOIN videos v ON v.id = q.active_video_id
                WHERE q.queue_name = 'main'
                """
            )
            post_queue_snapshot = _fetchone_dict(cur)
        conn.commit()

    if not version_already_applied:
        try:
            from bot.worker_kick import kick_worker_if_ready

            kick_worker_if_ready(reason="migration:1.0.18")
        except Exception:
            pass

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
            "telegram_updates_table": telegram_updates_table_exists,
            "background_jobs_table": background_jobs_table_exists,
            "bulk_operations_table": bulk_operations_table_exists,
            "worker_heartbeats_table": worker_heartbeats_table_exists,
            "worker_kick_state_table": worker_kick_state_table_exists,
            "schema_versions_table": schema_versions_table_exists,
            "schema_version_1_0_18": schema_version_applied,
            "atomic_queue_columns": atomic_queue_columns,
            "job_failure_columns": job_failure_columns,
            "sheet_sync_columns": sheet_sync_columns,
        },
        "queue_repair": {
            "action": repair_action,
            "stale_pending_message_metadata_cleared": stale_metadata_cleared,
            "before": pre_queue_snapshot,
            "after": post_queue_snapshot,
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
