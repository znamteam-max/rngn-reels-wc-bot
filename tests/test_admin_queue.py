from __future__ import annotations

import unittest
from datetime import date, datetime
from zoneinfo import ZoneInfo

from bot.handlers import (
    PENDING_VIDEOS_SQL,
    _date_iso,
    _edit_message_text_idempotent,
    _format_processed_queue_card,
    admin_queue_keyboard,
    format_admin_queue_card,
)
from bot.links import PUBLISH_DATE_ERROR, parse_publish_date
from bot.handlers import Actor
from bot.telegram import TelegramAPIError


class PublishDateParserV1011Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 7, 16, 16, 0, tzinfo=ZoneInfo("Europe/Helsinki"))

    def test_supported_formats_are_deterministic(self) -> None:
        expected = {
            "12.07": date(2026, 7, 12),
            "12.7": date(2026, 7, 12),
            "1.07": date(2026, 7, 1),
            "1.7": date(2026, 7, 1),
            "2026-07-12": date(2026, 7, 12),
            "Сегодня": date(2026, 7, 16),
            "Вчера": date(2026, 7, 15),
            "Позавчера": date(2026, 7, 14),
            "20.07": date(2026, 7, 20),
        }
        for raw, value in expected.items():
            with self.subTest(raw=raw):
                self.assertEqual(parse_publish_date(raw, now=self.now), value)

    def test_invalid_dates_use_one_exact_error(self) -> None:
        for raw in ("32.07", "12.13", "text", "2026-02-30"):
            with self.subTest(raw=raw), self.assertRaisesRegex(ValueError, PUBLISH_DATE_ERROR):
                parse_publish_date(raw, now=self.now)


class AdminQueuePresentationV1011Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.video = {
            "id": 52,
            "status": "pending",
            "video_type": "regular",
            "publish_date": date(2026, 7, 12),
            "instagram_url": "https://www.instagram.com/reel/ABC/",
            "youtube_url": None,
            "tiktok_url": "",
            "vk_url": "https://vk.com/clip-1_2",
            "author_name": "Автор",
            "author_username": "author",
            "montage_name": "Монтажёр",
            "montage_username": "editor",
            "voice_name": None,
            "added_by_username": "submitter",
            "added_by_tg_id": 7,
            "created_at": self._created_at(),
        }

    @staticmethod
    def _created_at() -> datetime:
        return datetime(2026, 7, 9, 15, 22, tzinfo=ZoneInfo("Europe/Helsinki"))

    def test_active_card_starts_with_id_and_hides_empty_links(self) -> None:
        text = format_admin_queue_card(self.video, total=34)
        self.assertTrue(text.startswith("Заявка #52\n"))
        self.assertIn("Очередь: 1 из 34", text)
        self.assertIn("Instagram: https://www.instagram.com/reel/ABC/", text)
        self.assertIn("VK: https://vk.com/clip-1_2", text)
        self.assertNotIn("YouTube:", text)
        self.assertNotIn("TikTok:", text)
        self.assertIn("Дата публикации: 12.07.2026", text)

    def test_keyboard_uses_video_scoped_global_callbacks(self) -> None:
        keyboard = admin_queue_keyboard(52)["inline_keyboard"]
        callbacks = [button["callback_data"] for row in keyboard for button in row]
        self.assertEqual(
            callbacks,
            [
                "admq:date:52",
                "admq:approve:52",
                "admq:revision:52",
                "admq:duplicate:52",
                "admq:delete:52",
                "admq:refresh:52",
            ],
        )
        self.assertTrue(all(not value.startswith("adm:") for value in callbacks))

    def test_processed_card_is_compact_and_identified(self) -> None:
        actor = Actor(tg_id=1, chat_id=-1001, username="znambo")
        text = _format_processed_queue_card(self.video, "approved", actor)
        self.assertEqual(
            text,
            "✅ Заявка #52 одобрена\nДата публикации: 12.07.2026\nПроверил: @znambo",
        )

    def test_pending_query_is_global_fifo(self) -> None:
        self.assertIn("WHERE v.status = 'pending'", PENDING_VIDEOS_SQL)
        self.assertIn("ORDER BY v.created_at ASC, v.id ASC", PENDING_VIDEOS_SQL)
        self.assertNotIn("batch_id", PENDING_VIDEOS_SQL)

    def test_repeated_identical_telegram_edit_is_successful(self) -> None:
        class NotModifiedTelegram:
            def edit_message_text(self, *args: object, **kwargs: object) -> None:
                raise TelegramAPIError("Bad Request: message is not modified", 400)

        _edit_message_text_idempotent(
            NotModifiedTelegram(),  # type: ignore[arg-type]
            -1001,
            212,
            "Заявка #52",
            {"inline_keyboard": []},
        )

    def test_audit_dates_are_json_serializable(self) -> None:
        self.assertEqual(_date_iso(date(2026, 7, 12)), "2026-07-12")
        self.assertEqual(_date_iso("2026-07-12"), "2026-07-12")
        self.assertIsNone(_date_iso(None))


if __name__ == "__main__":
    unittest.main()
