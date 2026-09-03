from __future__ import annotations

from typing import Any

import requests
from urllib3.util import Timeout

from bot.config import get_settings


LIVE_TELEGRAM_TIMEOUT = Timeout(connect=2, read=3, total=3.5)


class TelegramAPIError(RuntimeError):
    def __init__(
        self,
        description: str,
        status_code: int | None = None,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(f"Telegram API error: {description}")
        self.description = description
        self.status_code = status_code
        self.retry_after = retry_after


class TelegramClient:
    def __init__(self, *, timeout: float | tuple[float, float] = 15) -> None:
        self.settings = get_settings()
        if not self.settings.bot_token:
            raise RuntimeError("BOT_TOKEN is not configured")
        self.base_url = f"https://api.telegram.org/bot{self.settings.bot_token}"
        self.timeout = timeout

    @staticmethod
    def _parse_response(response: requests.Response) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError as exc:
            raise RuntimeError("Telegram API returned a non-JSON response") from exc
        if not data.get("ok"):
            description = data.get("description", "unknown Telegram API error")
            parameters = data.get("parameters") if isinstance(data.get("parameters"), dict) else {}
            retry_after = parameters.get("retry_after")
            raise TelegramAPIError(
                description,
                response.status_code,
                int(retry_after) if retry_after is not None else None,
            )
        return data

    def _request(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = requests.post(
                f"{self.base_url}/{method}",
                json=payload,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise RuntimeError("Telegram API request failed") from exc
        return self._parse_response(response)

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

    def send_document_bytes(
        self,
        chat_id: int | str,
        filename: str,
        content: bytes,
        *,
        caption: str | None = None,
        content_type: str = "text/csv",
    ) -> dict[str, Any]:
        data: dict[str, Any] = {"chat_id": str(chat_id)}
        if caption:
            data["caption"] = caption
        try:
            response = requests.post(
                f"{self.base_url}/sendDocument",
                data=data,
                files={"document": (filename, content, content_type)},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise RuntimeError("Telegram API request failed") from exc
        return self._parse_response(response)

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

    def edit_message_reply_markup(
        self,
        chat_id: int | str,
        message_id: int,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "reply_markup": reply_markup or {"inline_keyboard": []},
        }
        return self._request("editMessageReplyMarkup", payload)

    def delete_message(self, chat_id: int | str, message_id: int) -> dict[str, Any]:
        return self._request("deleteMessage", {"chat_id": chat_id, "message_id": message_id})

    def pin_chat_message(
        self,
        chat_id: int | str,
        message_id: int,
        disable_notification: bool = True,
    ) -> dict[str, Any]:
        return self._request(
            "pinChatMessage",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "disable_notification": disable_notification,
            },
        )

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
