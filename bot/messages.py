from __future__ import annotations

from datetime import date, datetime
from typing import Any


def user_label(username: str | None, tg_id: int | None) -> str:
    if username:
        return f"@{username}"
    return str(tg_id) if tg_id else "не указано"


def person_display(name: str | None, username: str | None) -> str:
    if not name:
        return "не указано"
    if username:
        return f"{name} (@{username})"
    return f"{name} (ник не указан)"


def person_value(row: dict[str, Any], prefix: str) -> str:
    name = row.get(f"{prefix}_name")
    username = row.get(f"{prefix}_username")
    if name:
        return person_display(str(name), str(username) if username else None)
    person = row.get(prefix)
    return str(person) if person else "не указано"


def _format_datetime(value: Any) -> str:
    if not value:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _format_date(value: Any) -> str:
    if not value:
        return ""
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _format_publish_date(value: Any) -> str:
    return _format_date(value) if value else "не указана"


def _link_value(value: Any) -> str:
    return str(value).strip() if value else "нет"


def _append_if(lines: list[str], label: str, value: Any) -> None:
    if value is None:
        return
    text = str(value).strip()
    if text:
        lines.append(f"{label}: {text}")


def format_video_card(
    row: dict[str, Any],
    title: str = "Заявка",
    position: str | None = None,
) -> str:
    header = title if not position else f"{title} {position}"
    lines = [
        header,
        f"ID: {row.get('id', 'новая')}",
        "",
        f"Instagram: {_link_value(row.get('instagram_url'))}",
        f"YouTube: {_link_value(row.get('youtube_url'))}",
        f"TikTok: {_link_value(row.get('tiktok_url'))}",
        f"VK: {_link_value(row.get('vk_url'))}",
        "",
        f"Дата публикации: {_format_publish_date(row.get('publish_date'))}",
        "",
        f"Автор: {person_value(row, 'author')}",
        f"Монтажёр: {person_value(row, 'montage')}",
        f"Озвучка: {person_value(row, 'voice') if row.get('voice_name') else 'нет'}",
        "",
        f"Добавил: {user_label(row.get('added_by_username'), row.get('added_by_tg_id'))}",
    ]
    if row.get("checked_by_username") or row.get("checked_by_tg_id"):
        lines.append(f"Проверил: {user_label(row.get('checked_by_username'), row.get('checked_by_tg_id'))}")
    _append_if(lines, "Комментарий", row.get("comment"))
    return "\n".join(lines)


def format_final_card(row: dict[str, Any]) -> str:
    lines = [
        "Видео одобрено и добавлено в отчёт",
        f"ID: {row.get('id', 'не указано')}",
        "",
        f"Дата публикации: {_format_publish_date(row.get('publish_date'))}",
        "",
        f"Instagram: {_link_value(row.get('instagram_url'))}",
        f"YouTube: {_link_value(row.get('youtube_url'))}",
        f"TikTok: {_link_value(row.get('tiktok_url'))}",
        f"VK: {_link_value(row.get('vk_url'))}",
        "",
        f"Автор: {person_value(row, 'author')}",
        f"Монтажёр: {person_value(row, 'montage')}",
        f"Озвучка: {person_value(row, 'voice') if row.get('voice_name') else 'нет'}",
        "",
        f"Добавил: {user_label(row.get('added_by_username'), row.get('added_by_tg_id'))}",
        f"Проверил: {user_label(row.get('checked_by_username'), row.get('checked_by_tg_id'))}",
    ]
    _append_if(lines, "Комментарий", row.get("comment"))
    return "\n".join(lines)


def format_batch_summary(batch: dict[str, Any]) -> str:
    return (
        f"Пачка #{batch['id']}: всего {batch.get('total_count', 0)}, "
        f"новых {batch.get('clean_count', 0)}, "
        f"возможных дублей {batch.get('duplicate_count', 0)}, "
        f"проблемных {batch.get('problem_count', 0)}"
    )
