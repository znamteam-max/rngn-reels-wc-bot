from __future__ import annotations

from typing import Any

from bot import admin_queue, db, jobs


WORLD_CUP_CODE = "world_cup_2026"
WORLD_CUP_NAME = "ЧМ 2026"
FINAL_VIDEO_IDS = (3, *range(325, 347))


def finalize_world_cup_2026() -> dict[str, Any]:
    """One-time, idempotent closeout of the final World Cup batch."""
    from bot import admin_tools

    admin_tools.ensure_payment_schema()
    target_ids = [int(value) for value in FINAL_VIDEO_IDS]

    with db.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtext('world_cup_2026_final_close'))")
            cur.execute(
                """
                SELECT id, status, project_code, project_name
                FROM videos
                WHERE id = ANY(%s)
                ORDER BY id
                FOR UPDATE
                """,
                (target_ids,),
            )
            rows = list(cur.fetchall())
            found_ids = {int(row["id"]) for row in rows}
            missing_ids = sorted(set(target_ids) - found_ids)
            if missing_ids:
                raise RuntimeError(f"World Cup finalization missing video IDs: {missing_ids}")

            unexpected = [
                int(row["id"])
                for row in rows
                if str(row.get("project_code") or "") not in {"ves_sport", WORLD_CUP_CODE}
                or str(row.get("status") or "") not in {"approved", "pending"}
            ]
            if unexpected:
                raise RuntimeError(f"World Cup finalization found unexpected rows: {unexpected}")

            already_done = all(
                str(row.get("project_code") or "") == WORLD_CUP_CODE
                and str(row.get("status") or "") == "approved"
                for row in rows
            )
            if already_done:
                return {
                    "ok": True,
                    "already_done": True,
                    "target_count": len(target_ids),
                    "moved": 0,
                    "approved_from_pending": 0,
                }

            cur.execute("SELECT id FROM projects WHERE code = %s", (WORLD_CUP_CODE,))
            project = cur.fetchone()
            if not project:
                raise RuntimeError("world_cup_2026 project is missing")
            world_cup_project_id = int(project["id"])

            pending_ids = [int(row["id"]) for row in rows if row.get("status") == "pending"]
            moved_ids = [
                int(row["id"])
                for row in rows
                if str(row.get("project_code") or "") == "ves_sport"
            ]

            cur.execute(
                """
                UPDATE videos
                SET project_id = %s,
                    project_code = %s,
                    project_name = %s,
                    status = 'approved',
                    checked_by_username = CASE
                        WHEN status = 'pending' THEN COALESCE(NULLIF(checked_by_username, ''), 'ZnamBo')
                        ELSE checked_by_username
                    END,
                    checked_at = CASE
                        WHEN status = 'pending' THEN COALESCE(checked_at, now())
                        ELSE checked_at
                    END,
                    updated_at = now()
                WHERE id = ANY(%s)
                """,
                (world_cup_project_id, WORLD_CUP_CODE, WORLD_CUP_NAME, target_ids),
            )

            # The user explicitly confirmed that no World Cup work has been paid yet.
            cur.execute(
                """
                INSERT INTO video_payments (
                    video_id, is_paid, paid_at, paid_by_tg_id, paid_by_username,
                    note, created_at, updated_at
                )
                SELECT
                    id, false, NULL, NULL, NULL,
                    'Final World Cup 2026 closeout: unpaid baseline',
                    now(), now()
                FROM videos
                WHERE status = 'approved'
                  AND project_code = %s
                ON CONFLICT (video_id) DO UPDATE SET
                    is_paid = false,
                    paid_at = NULL,
                    paid_by_tg_id = NULL,
                    paid_by_username = NULL,
                    note = EXCLUDED.note,
                    updated_at = now()
                """,
                (WORLD_CUP_CODE,),
            )

            for video_id in target_ids:
                jobs.enqueue_sheet_sync(
                    video_id,
                    version="world_cup_2026_final",
                    conn=conn,
                )
            jobs.enqueue_job(
                "sheets_sync_stats",
                {},
                dedupe_key="stats:projects",
                priority=70,
                conn=conn,
            )
            jobs.enqueue_admin_queue_pump(force_repost=False, conn=conn)
            jobs.enqueue_dashboard_refresh(conn=conn)

            db.log_event(
                conn,
                entity_type="project",
                entity_id=world_cup_project_id,
                action="world_cup_2026_finalized",
                actor_username="ZnamBo",
                before_data={
                    "ves_sport_target_ids": moved_ids,
                    "pending_target_ids": pending_ids,
                },
                after_data={
                    "project_code": WORLD_CUP_CODE,
                    "target_ids": target_ids,
                    "all_unpaid": True,
                    "closed": True,
                },
            )

    # These sheets are not maintained by sheets_sync_video jobs.
    reporting = admin_tools.sync_reporting_sheets()
    return {
        "ok": True,
        "already_done": False,
        "target_count": len(target_ids),
        "moved": len(moved_ids),
        "approved_from_pending": len(pending_ids),
        "reporting": reporting,
    }
