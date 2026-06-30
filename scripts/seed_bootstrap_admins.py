from __future__ import annotations

import os
import sys

import psycopg


def parse_ids(raw: str | None) -> list[int]:
    if not raw:
        return []
    ids: list[int] = []
    for item in raw.replace(";", ",").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            ids.append(int(item))
        except ValueError:
            continue
    return ids


def main() -> int:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL is not configured.", file=sys.stderr)
        return 1

    admin_ids = parse_ids(os.environ.get("BOOTSTRAP_SUPERADMIN_IDS"))
    admin_ids.extend(parse_ids(os.environ.get("ALLOWED_TELEGRAM_USER_IDS")))
    admin_ids = list(dict.fromkeys(admin_ids))

    if not admin_ids:
        print("No bootstrap admin IDs found in env.")
        return 2

    with psycopg.connect(database_url) as conn:
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
        conn.commit()

    print(f"Bootstrap superadmins seeded: {len(admin_ids)}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
