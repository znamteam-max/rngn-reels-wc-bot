from __future__ import annotations

from typing import Any

from bot import handlers as h
from bot import messages
from bot import project_workflow_patch as workflow


_INSTALLED = False


def is_aircut(video: dict[str, Any]) -> bool:
    return bool(video) and bool(workflow._is_aircut_video(video))


def visible_comment(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    marker = workflow.AIR_CUT_MARKER
    if text.startswith(marker):
        text = text[len(marker) :].lstrip()
    return text or None


def _display_video(video: dict[str, Any]) -> dict[str, Any]:
    row = dict(video)
    row["comment"] = visible_comment(video.get("comment"))
    return row


def _insert_aircut_type(text: str) -> str:
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.startswith("Тип:"):
            lines[index] = "Тип: отрез из эфира"
            return "\n".join(lines)
    for index, line in enumerate(lines):
        if line.startswith("Проект:"):
            lines.insert(index + 1, "Тип: отрез из эфира")
            return "\n".join(lines)
    return text


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    original_admin_queue_card = h.format_admin_queue_card
    original_video_card = h.format_video_card
    original_final_card = h.format_final_card

    def format_admin_queue_card(
        video: dict[str, Any],
        total: int,
        position: int = 1,
        queue_label: str = "Все проекты",
    ) -> str:
        text = original_admin_queue_card(_display_video(video), total, position, queue_label)
        return _insert_aircut_type(text) if is_aircut(video) else text

    def format_video_card(
        row: dict[str, Any],
        title: str = "Заявка",
        position: str | None = None,
    ) -> str:
        text = original_video_card(_display_video(row), title=title, position=position)
        return _insert_aircut_type(text) if is_aircut(row) else text

    def format_final_card(row: dict[str, Any]) -> str:
        text = original_final_card(_display_video(row))
        return _insert_aircut_type(text) if is_aircut(row) else text

    h.format_admin_queue_card = format_admin_queue_card
    h.format_video_card = format_video_card
    h.format_final_card = format_final_card
    messages.format_video_card = format_video_card
    messages.format_final_card = format_final_card
    _INSTALLED = True
