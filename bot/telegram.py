from __future__ import annotations

from typing import Any

import requests

from bot.config import get_settings


class TelegramClient:
    def __init__(self) -> None:
        self.settings = get_settings()
        if not self.settings.bot_token:
            raise RuntimeError("BOT_TOKEN is not configured")
        self.base_url = f"https://api.telegram.org/bot{self.settings.bot_token}"

    def _request(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = requests.post(
                f"{self.base_url}/{method}",
                json=payload,
                timeout=15,
            )
        except requests.RequestException as exc:
            raise RuntimeError("Telegram API request failed") from exc

        try:
            data = response.json()
        except ValueError as exc:
            raise RuntimeError("Telegram API returned a non-JSON response") from exc
        if not data.get("ok"):
            description = data.get("description", "unknown Telegram API error")
            raise RuntimeError(f"Telegram API error: {description}")
        return data

    def send_message(
        self,
        chat_id: int | str,
        text: str,
        reply_markup: dict[str, Any] | None = None,
        disable_web_page_preview: bool = True,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": disable_web_page_preview,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return self._request("sendMessage", payload)

    def edit_message_text(
        self,
        chat_id: int | str,
        message_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
        disable_web_page_preview: bool = True,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "disable_web_page_preview": disable_web_page_preview,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return self._request("editMessageText", payload)

    def answer_callback_query(
        self,
        callback_query_id: str,
        text: str | None = None,
        show_alert: bool = False,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "callback_query_id": callback_query_id,
            "show_alert": show_alert,
        }
        if text:
            payload["text"] = text
        return self._request("answerCallbackQuery", payload)


def inline_keyboard(rows: list[list[tuple[str, str]]]) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": text, "callback_data": callback_data} for text, callback_data in row]
            for row in rows
        ]
    }

