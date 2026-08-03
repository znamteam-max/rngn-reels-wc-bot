from __future__ import annotations

from typing import Any

from bot import db


EGOR_MONTAGE_NAME = "Егор Петрушков"
EGOR_MONTAGE_USERNAME = "RayBallPro"
EGOR_MONTAGE_ROLE = "montage"
EGOR_MONTAGE_SORT_WEIGHT = 20


def seed_egor_montage(conn) -> tuple[str, int, int]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id
            FROM people
            WHERE role = %s
              AND (
                lower(name) = lower(%s)
                OR lower(COALESCE(username, '')) = lower(%s)
              )
            ORDER BY
                CASE WHEN lower(name) = lower(%s) THEN 0 ELSE 1 END,
                CASE WHEN lower(COALESCE(username, '')) = lower(%s) THEN 0 ELSE 1 END,
                CASE WHEN is_active THEN 0 ELSE 1 END,
                id ASC
            FOR UPDATE
            """,
            (
                EGOR_MONTAGE_ROLE,
                EGOR_MONTAGE_NAME,
                EGOR_MONTAGE_USERNAME,
                EGOR_MONTAGE_NAME,
                EGOR_MONTAGE_USERNAME,
            ),
        )
        matches = list(cur.fetchall())
        if matches:
            person_id = int(matches[0][0])
            cur.execute(
                """
                UPDATE people
                SET name = %s,
                    username = %s,
                    is_active = true,
                    sort_weight = GREATEST(COALESCE(sort_weight, 0), %s)
                WHERE id = %s
                """,
                (
                    EGOR_MONTAGE_NAME,
                    EGOR_MONTAGE_USERNAME,
                    EGOR_MONTAGE_SORT_WEIGHT,
                    person_id,
                ),
            )
            duplicate_ids = [int(row[0]) for row in matches[1:]]
            if duplicate_ids:
                cur.execute(
                    """
                    UPDATE people
                    SET is_active = false
                    WHERE id = ANY(%s) AND role = %s
                    """,
                    (duplicate_ids, EGOR_MONTAGE_ROLE),
                )
            action = "updated"
        else:
            cur.execute(
                """
                INSERT INTO people (name, username, role, is_active, sort_weight)
                VALUES (%s, %s, %s, true, %s)
                RETURNING id
                """,
                (
                    EGOR_MONTAGE_NAME,
                    EGOR_MONTAGE_USERNAME,
                    EGOR_MONTAGE_ROLE,
                    EGOR_MONTAGE_SORT_WEIGHT,
                ),
            )
            person_id = int(cur.fetchone()[0])
            action = "inserted"

        cur.execute(
            """
            SELECT count(*)
            FROM people
            WHERE role = %s
              AND lower(name) = lower(%s)
              AND lower(COALESCE(username, '')) = lower(%s)
              AND is_active = true
            """,
            (EGOR_MONTAGE_ROLE, EGOR_MONTAGE_NAME, EGOR_MONTAGE_USERNAME),
        )
        active_rows = int(cur.fetchone()[0])
    return action, person_id, active_rows


def backfill_egor_montage_videos(conn, person_id: int) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE videos
            SET montage_username = %s,
                montage_id = %s,
                updated_at = now()
            WHERE montage_name = %s
              AND COALESCE(montage_username, '') = ''
            RETURNING id
            """,
            (EGOR_MONTAGE_USERNAME, person_id, EGOR_MONTAGE_NAME),
        )
        return len(cur.fetchall())


def seed_and_backfill_egor(conn) -> dict[str, Any]:
    action, person_id, active_rows = seed_egor_montage(conn)
    backfilled_count = backfill_egor_montage_videos(conn, person_id)
    if backfilled_count:
        db.log_event(
            conn,
            entity_type="videos",
            entity_id=None,
            action="egor_montage_backfilled",
            after_data={"count": backfilled_count},
        )
    return {
        "action": action,
        "person_id": person_id,
        "active_rows": active_rows,
        "backfilled_count": backfilled_count,
    }
