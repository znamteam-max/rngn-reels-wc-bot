from __future__ import annotations

import re
from typing import Any


PROJECTS: tuple[dict[str, Any], ...] = (
    {"code": "vzyal_myach", "name": "Взял Мяч", "emoji": "🏀", "sort_order": 10},
    {"code": "bolshe", "name": "Больше", "emoji": "🎾", "sort_order": 20},
    {"code": "ves_sport", "name": "Весь Спорт", "emoji": "🌍", "sort_order": 30},
    {"code": "padel_channel", "name": "Padel Channel", "emoji": "🎾", "sort_order": 40},
    {"code": "home_of_hockey", "name": "Home of Hockey", "emoji": "🏒", "sort_order": 50},
    {"code": "double_play", "name": "Double Play", "emoji": "🏈", "sort_order": 60},
    {"code": "sport_core", "name": "Sport Core", "emoji": "👕", "sort_order": 70},
    {"code": "music_core", "name": "Music Core", "emoji": "🎵", "sort_order": 80},
    {"code": "other", "name": "Другой проект", "emoji": "➕", "sort_order": 999},
)

PROJECT_BY_CODE = {str(project["code"]): project for project in PROJECTS}
PROJECT_SHEET_TITLES = {
    "vzyal_myach": "Взял Мяч",
    "bolshe": "Больше",
    "ves_sport": "Весь Спорт",
    "padel_channel": "Padel Channel",
    "home_of_hockey": "Home of Hockey",
    "double_play": "Double Play",
    "sport_core": "Sport Core",
    "music_core": "Music Core",
    "other": "Другие проекты",
}

_LINK_RE = re.compile(
    r"(?:https?://|www\.|t\.me/|instagram\.com/|youtu\.be/|youtube\.com/|vk\.com/)",
    re.IGNORECASE,
)


def seed_projects(conn) -> int:
    with conn.cursor() as cur:
        for project in PROJECTS:
            cur.execute(
                """
                INSERT INTO projects (code, name, emoji, is_active, sort_order, updated_at)
                VALUES (%s, %s, %s, true, %s, now())
                ON CONFLICT (code)
                DO UPDATE SET
                    name = EXCLUDED.name,
                    emoji = EXCLUDED.emoji,
                    is_active = true,
                    sort_order = EXCLUDED.sort_order,
                    updated_at = now()
                """,
                (
                    project["code"],
                    project["name"],
                    project["emoji"],
                    project["sort_order"],
                ),
            )
        cur.execute("SELECT count(*) FROM projects WHERE is_active = true")
        row = cur.fetchone()
    if isinstance(row, dict):
        return int(next(iter(row.values())))
    return int(row[0])


def normalize_custom_project_name(value: str) -> str | None:
    name = " ".join(value.strip().split())
    if len(name) < 2 or len(name) > 60 or _LINK_RE.search(name):
        return None
    return name


def project_sheet_title(code: str | None) -> str | None:
    return PROJECT_SHEET_TITLES.get(str(code or ""))
