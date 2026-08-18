from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler

from bot import admin_tools, db, reconciliation, sheets

MARKER = "world_cup_2026_finalized_2026_08_18"
REBUILD_MARKER = "world_cup_2026_final_rebuild_sync_334"


def _bytes(payload):
    return json.dumps(payload, ensure_ascii=False, default=str, separators=(",", ":")).encode("utf-8")


def _summary():
    return db.fetch_one(
        """
        SELECT
          count(*) FILTER (WHERE status <> 'deleted') AS active_total,
          count(*) FILTER (WHERE status='approved') AS approved_total,
          count(*) FILTER (WHERE status='pending') AS pending_total,
          count(*) FILTER (WHERE status='needs_revision') AS revision_total,
          count(*) FILTER (WHERE status='duplicate') AS duplicate_total,
          count(*) FILTER (WHERE status <> 'deleted' AND project_code='world_cup_2026') AS wc_total,
          count(*) FILTER (WHERE status <> 'deleted' AND project_code<>'world_cup_2026') AS outside_wc,
          count(*) FILTER (WHERE status='approved' AND video_type='bigrecap') AS bigrecap_total,
          count(*) FILTER (WHERE status='approved' AND COALESCE(video_type,'regular')<>'bigrecap') AS regular_total
        FROM videos
        """
    ) or {}


def _ensure_final_data():
    admin_tools.ensure_payment_schema()
    with db.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS data_migration_markers (
                    marker text PRIMARY KEY,
                    applied_at timestamptz NOT NULL DEFAULT now(),
                    details jsonb NULL
                )
                """
            )
            cur.execute("SELECT marker FROM data_migration_markers WHERE marker=%s", (MARKER,))
            already = bool(cur.fetchone())
            if not already:
                cur.execute("SELECT id FROM projects WHERE code='world_cup_2026' LIMIT 1")
                project = cur.fetchone()
                if not project:
                    raise RuntimeError("world_cup_2026 project is missing")
                project_id = int(project["id"])
                cur.execute(
                    """
                    UPDATE videos
                    SET project_id=%s,
                        project_code='world_cup_2026',
                        project_name='ЧМ 2026',
                        status=CASE WHEN status IN ('pending','needs_revision') THEN 'approved' ELSE status END,
                        sheet_sync_status='queued', sheet_sync_error=NULL, updated_at=now()
                    WHERE status <> 'deleted'
                    """,
                    (project_id,),
                )
                moved = int(cur.rowcount or 0)
                cur.execute(
                    """
                    UPDATE admin_queue_state
                    SET active_video_id=NULL, active_chat_id=NULL, active_message_id=NULL,
                        active_reservation_token=NULL, active_reserved_at=NULL,
                        claimed_by_tg_id=NULL, claimed_by_username=NULL, claimed_at=NULL,
                        last_repaired_at=now(), last_repair_reason='World Cup 2026 finalized', updated_at=now()
                    WHERE queue_name='main'
                    """
                )
                cur.execute(
                    """
                    INSERT INTO video_payments (
                        video_id,is_paid,paid_at,paid_by_tg_id,paid_by_username,note,created_at,updated_at
                    )
                    SELECT id,false,NULL,NULL,NULL,
                           'World Cup 2026 final baseline: nothing paid yet',now(),now()
                    FROM videos
                    WHERE status='approved' AND project_code='world_cup_2026'
                    ON CONFLICT (video_id) DO UPDATE SET
                        is_paid=false, paid_at=NULL, paid_by_tg_id=NULL, paid_by_username=NULL,
                        note=EXCLUDED.note, updated_at=now()
                    """
                )
                cur.execute(
                    "INSERT INTO data_migration_markers(marker,details) VALUES (%s,jsonb_build_object('moved',%s))",
                    (MARKER, moved),
                )
                db.log_event(
                    conn,
                    entity_type='project', entity_id=project_id,
                    action='world_cup_2026_finalized', after_data={'moved_active_records': moved},
                )
    return already


def _fresh_run_id():
    marker = db.fetch_one("SELECT details FROM data_migration_markers WHERE marker=%s", (REBUILD_MARKER,))
    if marker:
        details = marker.get('details') if isinstance(marker.get('details'), dict) else {}
        run_id = int(details.get('run_id') or 0)
        run = reconciliation.get_run(run_id)
        if run and run.get('status') == 'done':
            return run_id
    with db.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE sheet_reconciliation_runs
                SET status='cancelled', stage='superseded_by_final_world_cup_sync', finished_at=now(), updated_at=now()
                WHERE status IN ('created','auditing','awaiting_confirmation','rebuilding','validating')
                """
            )
            cur.execute(
                """
                INSERT INTO sheet_reconciliation_runs(
                    status,mode,initiated_by_tg_id,initiated_by_username,initiated_chat_id,
                    confirmed_by_tg_id,confirmed_by_username,confirmed_at,stage,started_at
                ) VALUES ('created','db_only',0,'system',0,0,'system',now(),'final_world_cup_sync',now())
                RETURNING id
                """
            )
            run_id = int(cur.fetchone()['id'])
            cur.execute(
                """
                INSERT INTO data_migration_markers(marker,details)
                VALUES (%s,jsonb_build_object('run_id',%s))
                ON CONFLICT (marker) DO UPDATE SET details=EXCLUDED.details, applied_at=now()
                """,
                (REBUILD_MARKER, run_id),
            )
    return run_id


def _sync_rebuild(run_id):
    service = sheets._service()
    run = reconciliation.get_run(run_id) or {}
    if run.get('status') == 'done':
        return run
    if run.get('status') == 'created':
        reconciliation.prepare_rebuild(run_id)
    run = reconciliation.get_run(run_id) or {}
    summary = run.get('summary') if isinstance(run.get('summary'), dict) else {}
    names = list(summary.get('sheet_names') or [])
    for index in range(len(names)):
        reconciliation.rebuild_sheet_chunk(run_id, index, service=service)
    result = reconciliation.validate_and_promote(run_id, service=service)
    admin_tools.sync_reporting_sheets(service=service)
    return result


def finalize():
    already = _ensure_final_data()
    summary = _summary()
    if int(summary.get('outside_wc') or 0) != 0:
        raise RuntimeError(f"outside_wc={summary.get('outside_wc')}")
    if int(summary.get('pending_total') or 0) != 0 or int(summary.get('revision_total') or 0) != 0:
        raise RuntimeError("unfinished World Cup records remain")
    run_id = _fresh_run_id()
    run = _sync_rebuild(run_id)
    return {
        'ok': True,
        'already_applied': already,
        'summary': summary,
        'reconciliation_run_id': run_id,
        'reconciliation_status': run.get('status'),
        'reconciliation': (run.get('summary') if isinstance(run.get('summary'), dict) else {}),
    }


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            payload = finalize()
            status = 200
        except Exception as exc:
            payload = {'ok': False, 'error': f'{type(exc).__name__}: {exc}'[:500]}
            status = 500
        body = _bytes(payload)
        self.send_response(status)
        self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Content-Length',str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):
        self.do_GET()
