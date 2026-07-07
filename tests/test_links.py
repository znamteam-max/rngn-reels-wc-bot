from __future__ import annotations

import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

from bot.config import get_settings, missing_env_names
from bot.handlers import (
    Actor,
    PENDING_VIDEOS_SQL,
    apply_montage_same_as_author,
    ask_people,
    build_chatid_text,
    normalize_video_type,
    start_new_bigrecap,
    telegram_failure_payload,
)
from bot.links import (
    normalize_instagram,
    normalize_tiktok,
    normalize_vk,
    normalize_youtube,
    parse_publish_date,
)
from bot.messages import format_video_card, person_display
from bot.sheets import SHEET_COLUMNS, video_to_row
from bot.telegram import TelegramAPIError
from scripts.seed_people import iter_rows


class FakeTelegram:
    def __init__(self) -> None:
        self.messages: list[dict[str, object]] = []

    def send_message(
        self,
        chat_id: int | str,
        text: str,
        reply_markup: dict[str, object] | None = None,
        disable_web_page_preview: bool = True,
    ) -> dict[str, object]:
        self.messages.append(
            {
                "chat_id": chat_id,
                "text": text,
                "reply_markup": reply_markup,
                "disable_web_page_preview": disable_web_page_preview,
            }
        )
        return {"ok": True, "result": {"message_id": len(self.messages)}}


class LinkNormalizationTests(unittest.TestCase):
    def test_instagram_reel_shortcode(self) -> None:
        link = normalize_instagram("https://www.instagram.com/reel/ABC123/?igsh=bad&utm_source=x")
        self.assertEqual(link.external_id, "ABC123")
        self.assertEqual(link.url, "https://www.instagram.com/reel/ABC123/")

    def test_youtube_short_url(self) -> None:
        link = normalize_youtube("https://youtu.be/abc_DEF-123?si=tracking")
        self.assertEqual(link.external_id, "abc_DEF-123")
        self.assertEqual(link.url, "https://youtu.be/abc_DEF-123")

    def test_youtube_shorts(self) -> None:
        link = normalize_youtube("https://youtube.com/shorts/xyz987?utm_campaign=x")
        self.assertEqual(link.external_id, "xyz987")
        self.assertEqual(link.url, "https://youtu.be/xyz987")

    def test_tiktok_video_id(self) -> None:
        link = normalize_tiktok("https://www.tiktok.com/@name/video/1234567890?utm_source=x")
        self.assertEqual(link.external_id, "1234567890")
        self.assertNotIn("utm_source", link.url)

    def test_vk_clip_id(self) -> None:
        link = normalize_vk("https://vk.com/clip-1_456?utm_source=x")
        self.assertEqual(link.external_id, "clip-1_456")
        self.assertNotIn("utm_source", link.url)

    def test_parse_dd_mm(self) -> None:
        value = parse_publish_date("01.07")
        self.assertEqual(value.month, 7)
        self.assertEqual(value.day, 1)

    def test_parse_d_m(self) -> None:
        value = parse_publish_date("3.7")
        self.assertEqual(value.month, 7)
        self.assertEqual(value.day, 3)

    def test_live_seed_role_counts(self) -> None:
        rows = iter_rows(Path("people.live-seed.csv"))
        counts = Counter(row["role"] for row in rows)
        self.assertEqual(counts["author"], 11)
        self.assertEqual(counts["montage"], 13)
        self.assertEqual(counts["voice"], 5)
        authors = {(row["name"], row["username"]) for row in rows if row["role"] == "author"}
        self.assertIn(("Прокудин", "ny_pochemu"), authors)


class BotV107Tests(unittest.TestCase):
    def tearDown(self) -> None:
        get_settings.cache_clear()

    def test_chatid_text_contains_chat_and_user_details(self) -> None:
        text = build_chatid_text(
            {"id": -100123, "type": "supergroup", "title": "RNGN Admin"},
            {"id": 42, "username": "admin"},
        )
        self.assertIn("chat_id: -100123", text)
        self.assertIn("chat_type: supergroup", text)
        self.assertIn("title: RNGN Admin", text)
        self.assertIn("from_id: 42", text)
        self.assertIn("from_username: @admin", text)

    def test_missing_env_does_not_require_work_chat_id(self) -> None:
        env = {
            "BOT_TOKEN": "token",
            "BOT_USERNAME": "rngn_reels_wc_bot",
            "WEBHOOK_SECRET": "secret",
            "DATABASE_URL": "postgresql://example",
            "ADMIN_CHAT_ID": "-1001",
            "GOOGLE_SERVICE_ACCOUNT_JSON_B64": "json",
            "GOOGLE_SHEETS_SPREADSHEET_ID": "sheet",
        }
        with patch.dict("os.environ", env, clear=True):
            get_settings.cache_clear()
            self.assertEqual(missing_env_names(), [])

    def test_self_montage_copies_author_snapshot(self) -> None:
        data = {
            "author_id": 7,
            "author_name": "Author Name",
            "author_username": "author_user",
        }
        updated = apply_montage_same_as_author(data)
        self.assertEqual(updated["montage_id"], 7)
        self.assertEqual(updated["montage_name"], "Author Name")
        self.assertEqual(updated["montage_username"], "author_user")
        self.assertTrue(updated["montage_same_as_author"])

    def test_person_display_includes_username_or_fallback(self) -> None:
        self.assertEqual(person_display("Ann", "ann"), "Ann (@ann)")
        self.assertEqual(person_display("Ann", None), "Ann (ник не указан)")

    def test_video_card_hides_empty_review_created_comment_lines(self) -> None:
        text = format_video_card(
            {
                "instagram_url": "https://www.instagram.com/reel/ABC/",
                "author_name": "Ann",
                "author_username": "ann",
                "montage_name": "Max",
                "montage_username": "max",
                "voice_name": None,
                "added_by_username": "lead",
                "added_by_tg_id": 1,
                "comment": "",
            }
        )
        self.assertIn("Автор: Ann (@ann)", text)
        self.assertIn("Монтажёр: Max (@max)", text)
        self.assertNotIn("Проверил:", text)
        self.assertNotIn("Создано", text)
        self.assertNotIn("Комментарий:", text)

    def test_pending_resend_query_targets_pending_only(self) -> None:
        self.assertIn("WHERE v.status = 'pending'", PENDING_VIDEOS_SQL)
        self.assertNotIn("WORK_CHAT_ID", PENDING_VIDEOS_SQL)

    def test_admin_failure_payload_keeps_telegram_diagnostics(self) -> None:
        payload = telegram_failure_payload(
            TelegramAPIError("Bad Request: chat not found", 400),
            -100123,
            "send_review_card",
        )
        self.assertEqual(payload["admin_chat_id"], -100123)
        self.assertEqual(payload["telegram_status_code"], 400)
        self.assertEqual(payload["telegram_description"], "Bad Request: chat not found")
        self.assertEqual(payload["stage"], "send_review_card")


class BotV108Tests(unittest.TestCase):
    def test_video_type_normalization_defaults_to_regular(self) -> None:
        self.assertEqual(normalize_video_type("bigrecap"), "bigrecap")
        self.assertEqual(normalize_video_type(None), "regular")
        self.assertEqual(normalize_video_type("other"), "regular")

    def test_bigrecap_card_shows_type_regular_card_hides_it(self) -> None:
        base = {
            "id": 10,
            "instagram_url": "https://www.instagram.com/reel/ABC/",
            "author_name": "Прокудин",
            "author_username": "ny_pochemu",
            "montage_name": "Max",
            "montage_username": "max",
            "added_by_username": "lead",
            "added_by_tg_id": 1,
        }
        bigrecap = format_video_card({**base, "video_type": "bigrecap"})
        regular = format_video_card({**base, "video_type": "regular"})
        self.assertIn("Тип: большой рекап", bigrecap)
        self.assertNotIn("Тип:", regular)

    def test_video_sheet_row_includes_video_type_after_status(self) -> None:
        row = video_to_row(
            {
                "id": 10,
                "status": "pending",
                "video_type": "bigrecap",
                "author_name": "Прокудин",
                "author_username": "ny_pochemu",
            }
        )
        self.assertEqual(SHEET_COLUMNS[:3], ["id", "status", "video_type"])
        self.assertEqual(row[:3], ["10", "pending", "bigrecap"])

    def test_start_new_bigrecap_stores_type_in_session(self) -> None:
        tg = FakeTelegram()
        actor = Actor(tg_id=1, chat_id=1, username="user")
        with patch("bot.handlers.db.set_session") as set_session:
            start_new_bigrecap(tg, actor)
        self.assertEqual(set_session.call_args.kwargs["state"], "new:instagram")
        self.assertEqual(set_session.call_args.kwargs["data"], {"video_type": "bigrecap"})
        self.assertIn("Instagram/Reels", tg.messages[0]["text"])

    def test_voice_prompt_uses_new_wording_and_no_button(self) -> None:
        tg = FakeTelegram()
        actor = Actor(tg_id=1, chat_id=1, username="user")
        with patch("bot.handlers.get_people", return_value=[]):
            ask_people(tg, actor, "voice")
        message = tg.messages[0]
        self.assertEqual(message["text"], "Была ли в ролике озвучка другого автора?")
        keyboard = message["reply_markup"]["inline_keyboard"]  # type: ignore[index]
        self.assertEqual(keyboard[0][0]["text"], "Нет, не было")


if __name__ == "__main__":
    unittest.main()
