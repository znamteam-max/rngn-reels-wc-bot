from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any

import psycopg

from scripts.init_db import SCHEMA_SQL
from scripts.seed_bootstrap_admins import parse_ids
from scripts.seed_people import iter_rows, upsert_person


ROOT = Path(__file__).resolve().parents[1]
PEOPLE_SEED = ROOT / "people.live-seed.csv"
SYNC_ROLES = ("author", "montage", "voice")


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _counts(cur: psycopg.Cursor) -> dict[str, Any]:
    tables = ["people", "videos", "batches", "logs", "user_sessions"]
    table_counts: dict[str, int] = {}
    for table in tables:
        cur.execute(f"SELECT count(*) FROM {table}")
        table_counts[table] = int(cur.fetchone()[0])

    cur.execute(
        """
        SELECT role,
               count(*) AS total_count,
               count(*) FILTER (WHERE is_active) AS active_count
        FROM people
        GROUP BY role
        ORDER BY role
        """
    )
    role_counts = {
        str(role): {"total": int(total_count), "active": int(active_count)}
        for role, total_count, active_count in cur.fetchall()
    }
    return {"tables": table_counts, "roles": role_counts}


def _deactivate_stale_people(conn: psycopg.Connection, rows: list[dict[str, Any]]) -> int:
    active_pairs = {
        ((row.get("role") or "").strip(), (row.get("name") or "").strip().lower())
        for row in rows
        if (row.get("role") or "").strip() in SYNC_ROLES and (row.get("name") or "").strip()
    }
    stale = 0
    with conn.cursor() as cur:
        for role in SYNC_ROLES:
            names = [name for pair_role, name in active_pairs if pair_role == role]
            cur.execute(
                """
                UPDATE people
                SET is_active = false
                WHERE role = %s
                  AND is_active = true
                  AND NOT (lower(name) = ANY(%s))
                """,
                (role, names),
            )
            stale += int(cur.rowcount or 0)
    return stale


class handler(BaseHTTPRequestHandler):
    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = _json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        expected_secret = os.environ.get("WEBHOOK_SECRET", "")
        actual_secret = self.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not expected_secret or actual_secret != expected_secret:
            self._send_json(401, {"ok": False, "error": "unauthorized"})
            return

        database_url = os.environ.get("DATABASE_URL", "")
        if not database_url:
            self._send_json(500, {"ok": False, "error": "DATABASE_URL is not configured"})
            return

        rows = iter_rows(PEOPLE_SEED)
        inserted = 0
        updated = 0
        admin_ids = parse_ids(os.environ.get("BOOTSTRAP_SUPERADMIN_IDS"))
        admin_ids.extend(parse_ids(os.environ.get("ALLOWED_TELEGRAM_USER_IDS")))
        admin_ids = list(dict.fromkeys(admin_ids))

        with psycopg.connect(database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(SCHEMA_SQL)

            for row in rows:
                action, _ = upsert_person(conn, row)
                inserted += int(action == "inserted")
                updated += int(action == "updated")

            deactivated = _deactivate_stale_people(conn, rows)

            with conn.cursor() as cur:
                for index, tg_id in enumerate(admin_ids, start=1):
                    cur.execute(
                        """
                        INSERT INTO people (name, tg_id, role, is_active, sort_weight)
                        SELECT %s, %s, 'superadmin', true, 100
                        WHERE NOT EXISTS (
                            SELECT 1 FROM people
                            WHERE tg_id = %s
                              AND role IN ('admin', 'superadmin')
                        )
                        """,
                        (f"Bootstrap Superadmin {index}", tg_id, tg_id),
                    )
                result_counts = _counts(cur)
            conn.commit()

        self._send_json(
            200,
            {
                "ok": True,
                "people_seed": {"inserted": inserted, "updated": updated, "deactivated": deactivated},
                "bootstrap_admin_ids_present": len(admin_ids),
                "counts": result_counts,
            },
        )

    def do_GET(self) -> None:
        self._send_json(405, {"ok": False, "error": "method not allowed"})
