from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler
from typing import Any
from urllib.parse import parse_qs, urlparse

from bot import db, reconciliation, sheets
from bot.config import get_settings
from bot.github_oidc import GitHubOIDCError, validate_github_oidc_token
from bot.job_worker import process_jobs

WC_CODE = "world_cup_2026"
EGOR_NAME = "Егор Петрушков"
EGOR_USERNAME = "RayBallPro"


def _bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")


def _row_dicts(table: list[list[Any]]) -> list[dict[str, str]]:
    if not table:
        return []
    header_index = None
    for index, row in enumerate(table[:8]):
        values = [str(value).strip() for value in row]
        if "id" in values:
            header_index = index
            break
    if header_index is None:
        return []
    header = [str(value).strip() for value in table[header_index]]
    rows: list[dict[str, str]] = []
    for row in table[header_index + 1 :]:
        if not row:
            continue
        values = [str(value).strip() for value in row]
        item = {column: values[i] if i < len(values) else "" for i, column in enumerate(header)}
        if item.get("id", "").isdigit():
            rows.append(item)
    return rows


def _is_levchenko(video: dict[str, Any]) -> bool:
    name = str(video.get("author_name") or "").casefold()
    username = str(video.get("author_username") or "").lstrip("@").casefold()
    return "левченко" in name or username == "milovaantseva"


def _is_hamidulin(video: dict[str, Any]) -> bool:
    name = str(video.get("author_name") or "").casefold()
    username = str(video.get("author_username") or "").lstrip("@").casefold()
    return "хамидулин" in name or username == "kkkkkk13_13"


def _manual_sheet_state() -> dict[str, Any]:
    tables = sheets.read_named_tables([sheets.SHEET_NAME, "ЧМ 2026"])
    parsed = {name: _row_dicts(table) for name, table in tables.items()}
    by_sheet = {
        name: {int(row["id"]): row for row in rows}
        for name, rows in parsed.items()
    }
    return {"rows": parsed, "by_sheet": by_sheet}


def _merge_egor(conn) -> dict[str, Any]:
    result: dict[str, Any] = {"roles": {}}
    for role in ("author", "montage", "voice"):
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, name, username, tg_id, role, is_active
                FROM people
                WHERE role=%s
                  AND (
                    lower(name) IN ('егор', 'егор петрушков')
                    OR lower(COALESCE(username,''))='rayballpro'
                  )
                ORDER BY
                    CASE WHEN lower(COALESCE(username,''))='rayballpro' THEN 0 ELSE 1 END,
                    CASE WHEN lower(name)='егор петрушков' THEN 0 ELSE 1 END,
                    CASE WHEN is_active THEN 0 ELSE 1 END,
                    id
                FOR UPDATE
                """,
                (role,),
            )
            rows = list(cur.fetchall())
            if not rows:
                result["roles"][role] = {"matched": 0}
                continue
            canonical = rows[0]
            canonical_id = int(canonical["id"])
            duplicate_ids = [int(row["id"]) for row in rows[1:]]
            if duplicate_ids:
                cur.execute(
                    """
                    UPDATE people
                    SET is_active=false,
                        username=CASE WHEN lower(COALESCE(username,''))='rayballpro' THEN NULL ELSE username END
                    WHERE id=ANY(%s)
                    """,
                    (duplicate_ids,),
                )
            cur.execute(
                """
                UPDATE people
                SET name=%s, username=%s, is_active=true
                WHERE id=%s
                """,
                (EGOR_NAME, EGOR_USERNAME, canonical_id),
            )
            id_col = f"{role}_id"
            name_col = f"{role}_name"
            username_col = f"{role}_username"
            cur.execute(
                f"""
                UPDATE videos
                SET {id_col}=%s,
                    {name_col}=%s,
                    {username_col}=%s,
                    sheet_sync_status='queued',
                    sheet_sync_error=NULL,
                    updated_at=now()
                WHERE status <> 'deleted'
                  AND (
                    {id_col}=ANY(%s)
                    OR lower(COALESCE({name_col},'')) IN ('егор','егор петрушков')
                    OR lower(COALESCE({username_col},''))='rayballpro'
                  )
                """,
                (canonical_id, EGOR_NAME, EGOR_USERNAME, [canonical_id, *duplicate_ids]),
            )
            result["roles"][role] = {
                "matched": len(rows),
                "canonical_id": canonical_id,
                "deactivated_ids": duplicate_ids,
                "video_rows_updated": int(cur.rowcount or 0),
            }
    return result


def _apply_manual_corrections(conn, sheet_state: dict[str, Any]) -> dict[str, Any]:
    videos = reconciliation.load_active_video_snapshot(conn)
    wc = [video for video in videos if str(video.get("project_code") or "") == WC_CODE]
    by_id = {int(video["id"]): video for video in wc}
    sheet_maps = sheet_state["by_sheet"]
    usable_sets = [set(rows) for rows in sheet_maps.values() if len(rows) >= max(1, int(len(wc) * 0.8))]

    lev_all = [video for video in wc if _is_levchenko(video)]
    lev_missing = [
        video for video in lev_all
        if usable_sets and any(int(video["id"]) not in ids for ids in usable_sets)
    ]
    lev_candidates = lev_missing
    lev_reason = "missing_from_current_sheet"
    if len(lev_candidates) != 1:
        lev_candidates = [
            video for video in lev_all
            if str(video.get("status") or "") == "needs_revision" or video.get("publish_date") is None
        ]
        lev_reason = "single_unfinished_levchenko"
    lev_deleted_id = None
    if len(lev_candidates) == 1:
        lev_deleted_id = int(lev_candidates[0]["id"])
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE videos
                SET status='deleted', sheet_sync_status='queued', sheet_sync_error=NULL, updated_at=now()
                WHERE id=%s AND status <> 'deleted'
                """,
                (lev_deleted_id,),
            )
        db.log_event(
            conn,
            entity_type="video",
            entity_id=lev_deleted_id,
            action="user_confirmed_test_deleted",
            actor_username="system:report-v1.0.20",
            after_data={"reason": lev_reason, "author": "Левченко"},
        )

    # Prefer the row the user explicitly changed to bigrecap in either Videos or ЧМ 2026.
    observed_by_id: dict[int, dict[str, str]] = {}
    for sheet_name in (sheets.SHEET_NAME, "ЧМ 2026"):
        for video_id, row in sheet_maps.get(sheet_name, {}).items():
            if row.get("video_type", "").strip().lower() == "bigrecap":
                observed_by_id[video_id] = row
    ham_candidates = [
        video for video in wc
        if _is_hamidulin(video)
        and int(video["id"]) in observed_by_id
        and str(video.get("video_type") or "regular").lower() != "bigrecap"
    ]
    ham_reason = "sheet_marked_bigrecap"
    if len(ham_candidates) != 1:
        ham_candidates = [
            video for video in wc
            if _is_hamidulin(video)
            and (
                str(video.get("status") or "") == "needs_revision"
                or video.get("publish_date") is None
            )
        ]
        ham_reason = "single_unfinished_hamidulin"
    hamidulin_id = None
    hamidulin_applied: dict[str, Any] = {}
    if len(ham_candidates) == 1:
        video = ham_candidates[0]
        hamidulin_id = int(video["id"])
        observed = observed_by_id.get(hamidulin_id, {})
        status = str(observed.get("status") or video.get("status") or "").strip()
        publish_date = str(observed.get("publish_date") or "").strip()
        youtube_url = str(observed.get("youtube_url") or "").strip()
        youtube_id = str(observed.get("youtube_id") or "").strip()
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE videos
                SET video_type='bigrecap',
                    status=CASE WHEN %s='approved' THEN 'approved' ELSE status END,
                    publish_date=CASE WHEN %s<>'' THEN %s::date ELSE publish_date END,
                    youtube_url=CASE WHEN %s<>'' THEN %s ELSE youtube_url END,
                    youtube_id=CASE WHEN %s<>'' THEN %s ELSE youtube_id END,
                    sheet_sync_status='queued', sheet_sync_error=NULL, updated_at=now()
                WHERE id=%s AND status <> 'deleted'
                """,
                (
                    status,
                    publish_date,
                    publish_date or None,
                    youtube_url,
                    youtube_url or None,
                    youtube_id,
                    youtube_id or None,
                    hamidulin_id,
                ),
            )
        hamidulin_applied = {
            "reason": ham_reason,
            "sheet_status": status,
            "sheet_publish_date": publish_date,
            "sheet_youtube": bool(youtube_url or youtube_id),
        }
        db.log_event(
            conn,
            entity_type="video",
            entity_id=hamidulin_id,
            action="user_confirmed_bigrecap",
            actor_username="system:report-v1.0.20",
            after_data={"video_type": "bigrecap", **hamidulin_applied},
        )

    return {
        "levchenko_deleted_id": lev_deleted_id,
        "levchenko_candidate_count": len(lev_candidates),
        "hamidulin_bigrecap_id": hamidulin_id,
        "hamidulin_candidate_count": len(ham_candidates),
        "hamidulin_applied": hamidulin_applied,
    }


def _apply() -> dict[str, Any]:
    sheet_state = _manual_sheet_state()
    with db.transaction() as conn:
        corrections = _apply_manual_corrections(conn, sheet_state)
        egor = _merge_egor(conn)
        db.log_event(
            conn,
            entity_type="sheet_reconciliation",
            entity_id=None,
            action="report_v1_0_20_source_cleanup",
            actor_username="system:report-v1.0.20",
            after_data={"manual_corrections": corrections, "egor_merge": egor},
        )
    settings = get_settings()
    run_id = reconciliation.create_audit_run(
        actor_tg_id=0,
        actor_username="system:report-v1.0.20",
        chat_id=int(settings.admin_chat_id),
    )
    return {
        "ok": True,
        "run_id": run_id,
        "manual_corrections": corrections,
        "egor_merge": egor,
        "status": _status(),
    }


def _confirm() -> dict[str, Any]:
    run = reconciliation.get_run()
    if not run:
        raise RuntimeError("reconciliation run not found")
    confirmed = False
    if run.get("status") == "awaiting_confirmation":
        confirmed = reconciliation.confirm_run(
            int(run["id"]),
            mode="db_only",
            actor_tg_id=0,
            actor_username="system:report-v1.0.20",
        )
    return {"ok": True, "confirmed_now": confirmed, "status": _status()}


def _managed_preamble_status(service) -> dict[str, Any]:
    settings = get_settings()
    spreadsheet_id = settings.google_sheets_spreadsheet_id
    properties = sheets._sheet_properties(service, spreadsheet_id)
    names = [
        sheets.SHEET_NAME,
        *[title for title in sheets.PROJECT_SHEET_TITLES.values() if title in properties],
        *sorted(title for title in properties if reconciliation.MONTH_RE.match(title)),
        reconciliation.NO_DATE_SHEET,
        sheets.PROJECT_STATS_SHEET_NAME,
        sheets.MONTH_STATS_SHEET_NAME,
        sheets.AUTHOR_WORK_SHEET_NAME,
        sheets.MONTAGE_WORK_SHEET_NAME,
        sheets.UNFINISHED_SHEET_NAME,
        sheets.UNSUBMITTED_SHEET_NAME,
        sheets.RECONCILIATION_SHEET_NAME,
        sheets.BACKFILL_REVIEW_SHEET_NAME,
        sheets.METRICS_SHEET_NAME,
    ]
    names = [name for name in dict.fromkeys(names) if name in properties]
    tables = sheets.read_named_tables(names, service=service)
    failures: list[str] = []
    for name in names:
        table = tables.get(name) or []
        first = [str(row[0]).strip() if row else "" for row in table[:4]]
        if len(first) < 4 or first[:3] != ["Что показывает", "Зачем нужна", "Важно"]:
            failures.append(name)
            continue
        if not first[3]:
            failures.append(name)
    return {"checked": len(names), "failures": failures}


def _postprocess() -> dict[str, Any]:
    run = reconciliation.get_run()
    if not run or run.get("status") != "done":
        raise RuntimeError("reconciliation is not done")
    service = sheets._service()
    videos = reconciliation.load_active_video_snapshot()
    sheets.normalize_metrics_sheet_layout(service=service)
    deleted_tabs = sheets.cleanup_empty_report_tabs(videos, service=service)
    preambles = _managed_preamble_status(service)
    return {
        "ok": not preambles["failures"],
        "deleted_tabs": deleted_tabs,
        "preambles": preambles,
        "status": _status(service=service),
    }


def _status(*, service=None) -> dict[str, Any]:
    run = reconciliation.get_run() or {}
    summary = run.get("summary") if isinstance(run.get("summary"), dict) else {}
    videos = reconciliation.load_active_video_snapshot()
    approved = [video for video in videos if video.get("status") == "approved"]
    regular = sum(str(video.get("video_type") or "regular").lower() != "bigrecap" for video in approved)
    bigrecap = sum(str(video.get("video_type") or "regular").lower() == "bigrecap" for video in approved)
    egor_rows = db.fetch_all(
        """
        SELECT id,name,username,role,is_active
        FROM people
        WHERE lower(name) IN ('егор','егор петрушков')
           OR lower(COALESCE(username,''))='rayballpro'
        ORDER BY role,id
        """
    )
    author_rows = reconciliation.build_author_work_rows(videos)
    montage_rows = reconciliation.build_montage_work_rows(videos)
    payload = {
        "ok": True,
        "run_id": int(run["id"]) if run else None,
        "run_status": run.get("status"),
        "run_stage": run.get("stage"),
        "mismatch_count": reconciliation.run_mismatch_count(run) if run else None,
        "db_active_count": len(videos),
        "approved_count": len(approved),
        "approved_regular": int(regular),
        "approved_bigrecap": int(bigrecap),
        "needs_revision": sum(video.get("status") == "needs_revision" for video in videos),
        "missing_date": sum(reconciliation.publish_month(video) is None for video in videos),
        "project_sheet_counts": summary.get("project_sheet_counts") or {},
        "month_sheet_counts": summary.get("month_sheet_counts") or {},
        "egor_people_rows": egor_rows,
        "author_report_all": [row for row in author_rows if row and row[0] == "ALL"],
        "montage_report_all": [row for row in montage_rows if row and row[0] == "ALL"],
        "finished_at": run.get("finished_at").isoformat() if run and run.get("finished_at") else None,
        "last_error": run.get("last_error"),
    }
    if service is not None:
        payload["preambles"] = _managed_preamble_status(service)
    return payload


class handler(BaseHTTPRequestHandler):
    def _send(self, code: int, payload: dict[str, Any]) -> None:
        body = _bytes(payload)
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth(self) -> bool:
        auth = self.headers.get("Authorization") or ""
        scheme, _, token = auth.partition(" ")
        if scheme.lower() != "bearer" or token.count(".") != 2:
            return False
        try:
            validate_github_oidc_token(token)
            return True
        except GitHubOIDCError:
            return False

    def do_GET(self) -> None:
        if not self._auth():
            self._send(401, {"ok": False, "error": "unauthorized"})
            return
        action = parse_qs(urlparse(self.path).query).get("action", ["status"])[0]
        try:
            if action == "status":
                payload = _status()
            elif action == "apply":
                payload = _apply()
            elif action == "confirm":
                payload = _confirm()
            elif action == "drain":
                payload = {"ok": True, "worker": process_jobs(source="github_actions"), "status": _status()}
            elif action == "postprocess":
                payload = _postprocess()
            else:
                self._send(400, {"ok": False, "error": "unsupported action"})
                return
        except Exception as exc:
            self._send(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:800]})
            return
        self._send(200, payload)

    def do_POST(self) -> None:
        self.do_GET()
