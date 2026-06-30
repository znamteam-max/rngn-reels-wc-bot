from __future__ import annotations

from datetime import date, datetime
from typing import Any


def person_value(row: dict[str, Any], prefix: str) -> str:
    name = row.get(f"{prefix}_name")
    if name:
        return str(name)
    person = row.get(prefix)
    return str(person) if person else "не указано"


def user_label(username: str | None, tg_id: int | None) -> str:
    if username:
        return f"@{username}"
    return str(tg_id) if tg_id else "не указано"


def _format_datetime(value: Any) -> str:
    if not value:
        return "не указано"
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _format_date(value: Any) -> str:
    if not value:
        return "не указано"
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _format_publish_date(value: Any) -> str:
    return _format_date(value) if value else "не указана"


def _line(label: str, value: Any) -> str:
    value_text = str(value).strip() if value else "нет"
    return f"{label}: {value_text}"


def format_video_card(
    row: dict[str, Any],
    title: str = "Заявка",
    position: str | None = None,
) -> str:
    header = title
    if position:
        header = f"{title} {position}"
    return "\n".join(
        [
            header,
            f"ID: {row.get('id', 'новая')}",
            _line("Статус", row.get("status")),
            _line("Дата публикации", _format_publish_date(row.get("publish_date"))),
            _line("Instagram", row.get("instagram_url")),
            _line("Instagram ID", row.get("instagram_id")),
            _line("YouTube", row.get("youtube_url")),
            _line("TikTok", row.get("tiktok_url")),
            _line("VK", row.get("vk_url")),
            _line("Автор", row.get("author_name")),
            _line("Монтажёр", row.get("montage_name")),
            _line("Озвучка", row.get("voice_name") or "нет"),
            _line("Добавил", user_label(row.get("added_by_username"), row.get("added_by_tg_id"))),
            _line("Проверил", user_label(row.get("checked_by_username"), row.get("checked_by_tg_id"))),
            _line("Создано", _format_datetime(row.get("created_at"))),
            _line("Комментарий", row.get("comment")),
        ]
    )


def format_final_card(row: dict[str, Any]) -> str:
    return "\n".join(
        [
            "Видео проверено и добавлено в отчёт",
            _line("Дата публикации", _format_publish_date(row.get("publish_date"))),
            _line("Instagram", row.get("instagram_url")),
            _line("YouTube", row.get("youtube_url")),
            _line("TikTok", row.get("tiktok_url")),
            _line("VK", row.get("vk_url")),
            _line("Автор", row.get("author_name")),
            _line("Монтажёр", row.get("montage_name")),
            _line("Озвучка", row.get("voice_name") or "нет"),
            _line("Проверил", user_label(row.get("checked_by_username"), row.get("checked_by_tg_id"))),
        ]
    )


def format_batch_summary(batch: dict[str, Any]) -> str:
    return (
        f"Пачка #{batch['id']}: всего {batch.get('total_count', 0)}, "
        f"новых {batch.get('clean_count', 0)}, "
        f"возможных дублей {batch.get('duplicate_count', 0)}, "
        f"проблемных {batch.get('problem_count', 0)}"
    )
