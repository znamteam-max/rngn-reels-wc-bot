from __future__ import annotations

from typing import Any

from bot import db, jobs, reconciliation, sheets
from bot.config import get_settings


WORLD_CUP_CODE = "world_cup_2026"
WORLD_CUP_NAME = "ЧМ 2026"
FINAL_VIDEO_IDS = (3, *range(325, 347))


def _sync_final_work_sheets() -> dict[str, int]:
    settings = get_settings()
    if not settings.google_sheets_spreadsheet_id:
        raise RuntimeError("GOOGLE_SHEETS_SPREADSHEET_ID is not configured")
    service = sheets._service()
    spreadsheet_id = settings.google_sheets_spreadsheet_id
    videos = [
        video
        for video in reconciliation.load_active_video_snapshot()
        if str(video.get("project_code") or "") == WORLD_CUP_CODE
    ]
    author_rows = reconciliation.build_author_work_rows(videos)
    montage_rows = reconciliation.build_montage_work_rows(videos)
    sheets._ensure_named_sheets(
        service,
        spreadsheet_id,
        {
            reconciliation.AUTHOR_WORK_SHEET_NAME: reconciliation.AUTHOR_WORK_COLUMNS,
            reconciliation.MONTAGE_WORK_SHEET_NAME: reconciliation.MONTAGE_WORK_COLUMNS,
        },
    )
    sheets._replace_named_sheet(
        service,
        spreadsheet_id,
        reconciliation.AUTHOR_WORK_SHEET_NAME,
        reconciliation.AUTHOR_WORK_COLUMNS,
        author_rows,
    )
    sheets._replace_named_sheet(
        service,
        spreadsheet_id,
        reconciliation.MONTAGE_WORK_SHEET_NAME,
        reconciliation.MONTAGE_WORK_COLUMNS,
        montage_rows,
    )
    values = service.spreadsheets().values()
    values.update(
        spreadsheetId=spreadsheet_id,
        range="'Работа авторов'!A1:B1",
        valueInputOption="RAW",
        body={
            "values": [[
                "О вкладке",
                "Финальный отчёт ЧМ 2026 по авторам. Строка ALL = весь закрытый проект ЧМ 2026; будущие ролики других проектов сюда не попадут.",
            ]]
        },
    ).execute()
    values.update(
        spreadsheetId=spreadsheet_id,
        range="'Монтаж — справочно'!A1:B1",
        valueInputOption="RAW",
        body={
            "values": [[
                "О вкладке",
                "Финальный справочный отчёт ЧМ 2026 по монтажу. Строка ALL = весь закрытый проект ЧМ 2026.",
            ]]
        },
    ).execute()
    return {
        "world_cup_videos": len(videos),
        "author_rows": len(author_rows),
        "montage_rows": len(montage_rows),
    }


def finalize_world_cup_2026() -> dict[str, Any]:
    """One-time, idempotent closeout of the final World Cup batch."""
    from bot import admin_tools

    admin_tools.ensure_payment_schema()
    target_ids = [int(value) for value in FINAL_VIDEO_IDS]
    already_done = False
    moved_ids: list[int] = []
    pending_ids: list[int] = []

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

            if not already_done:
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

    reporting = admin_tools.sync_reporting_sheets()
    work_sheets = _sync_final_work_sheets()
    return {
        "ok": True,
        "already_done": already_done,
        "target_count": len(target_ids),
        "moved": len(moved_ids),
        "approved_from_pending": len(pending_ids),
        "reporting": reporting,
        "work_sheets": work_sheets,
    }
