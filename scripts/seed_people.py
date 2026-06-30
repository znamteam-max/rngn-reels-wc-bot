from __future__ import annotations

import argparse
import csv
import os
import sys
from pathlib import Path
from typing import Any

import psycopg


ROLES = {"author", "montage", "voice", "admin", "superadmin"}


def normalize_username(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    if not value:
        return None
    return value[1:] if value.startswith("@") else value


def parse_int(value: str | None) -> int | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    return int(value)


def iter_rows(path: Path, default_role: str | None = None) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8-sig")
    lines = [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]
    if not lines:
        return []

    first = lines[0].lower().replace(";", ",")
    rows: list[dict[str, Any]] = []
    if "name" in first and "role" in first:
        delimiter = ";" if ";" in lines[0] else ","
        reader = csv.DictReader(lines, delimiter=delimiter)
        for row in reader:
            rows.append(row)
        return rows

    for line in lines:
        parts = [part.strip() for part in line.replace(";", ",").split(",")]
        if default_role:
            role = default_role
            name = parts[0]
            rest = parts[1:]
        else:
            role = parts[0]
            name = parts[1] if len(parts) > 1 else ""
            rest = parts[2:]
        rows.append(
            {
                "role": role,
                "name": name,
                "tg_id": rest[0] if len(rest) > 0 else "",
                "username": rest[1] if len(rest) > 1 else "",
                "sort_weight": rest[2] if len(rest) > 2 else "0",
            }
        )
    return rows


def upsert_person(conn, row: dict[str, Any]) -> tuple[str, int]:
    role = (row.get("role") or "").strip()
    name = (row.get("name") or "").strip()
    if role not in ROLES:
        raise ValueError(f"Unknown role: {role}")
    if not name:
        raise ValueError("Name is required")
    tg_id = parse_int(row.get("tg_id"))
    username = normalize_username(row.get("username"))
    sort_weight = parse_int(row.get("sort_weight")) or 0
    is_active = str(row.get("is_active", "true")).strip().lower() not in {"0", "false", "no", "нет"}

    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM people WHERE lower(name) = lower(%s) AND role = %s LIMIT 1",
            (name, role),
        )
        existing = cur.fetchone()
        if existing:
            person_id = int(existing[0])
            cur.execute(
                """
                UPDATE people
                SET tg_id = COALESCE(%s, tg_id),
                    username = COALESCE(%s, username),
                    sort_weight = %s,
                    is_active = %s
                WHERE id = %s
                """,
                (tg_id, username, sort_weight, is_active, person_id),
            )
            return "updated", person_id
        cur.execute(
            """
            INSERT INTO people (name, tg_id, username, role, is_active, sort_weight)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (name, tg_id, username, role, is_active, sort_weight),
        )
        return "inserted", int(cur.fetchone()[0])


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed people for the RNGN Reels Telegram bot.")
    parser.add_argument("file", type=Path, help="CSV or simple comma-separated list")
    parser.add_argument("--role", choices=sorted(ROLES), help="Default role for simple one-name-per-line files")
    args = parser.parse_args()

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL is not configured.", file=sys.stderr)
        return 1

    rows = iter_rows(args.file, args.role)
    inserted = 0
    updated = 0
    with psycopg.connect(database_url) as conn:
        for row in rows:
            action, _ = upsert_person(conn, row)
            inserted += int(action == "inserted")
            updated += int(action == "updated")
        conn.commit()
    print(f"People seed complete. Inserted: {inserted}, updated: {updated}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
