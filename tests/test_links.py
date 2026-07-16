from __future__ import annotations

import unittest
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from bot.config import get_settings, missing_env_names
from bot.handlers import (
    ADD_ZNAMBO_DATE_PROMPT,
    ADD_ZNAMBO_INVALID_DATE_MESSAGE,
    ADD_ZNAMBO_INVALID_LINK_MESSAGE,
    ADD_ZNAMBO_LINK_PROMPT,
    ADD_ZNAMBO_MANUAL_DATE_PROMPT,
    ADD_ZNAMBO_SESSION_DATE,
    ADD_ZNAMBO_SESSION_INSTAGRAM,
    ADD_ZNAMBO_UNAUTHORIZED_MESSAGE,
    Actor,
    BIGRECAP_YOUTUBE_INVALID_MESSAGE,
    BIGRECAP_YOUTUBE_PROMPT,
    PENDING_VIDEOS_SQL,
    _send_main_menu,
    apply_montage_same_as_author,
    ask_people,
    build_chatid_text,
    format_add_znambo_success,
    handle_add_znambo_date,
    handle_new_bigrecap_youtube,
    handle_add_znambo_instagram,
    handle_callback,
    normalize_video_type,
    normalized_submission_data,
    next_after_person,
    parse_add_znambo_date,
    start_add_znambo,
    start_add_znambo_manual_date,
    start_new_video,
    start_new_bigrecap,
    telegram_failure_payload,
)
from bot.links import (
    extract_youtube_id,
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

    def test_extract_youtube_id_mobile_watch(self) -> None:
        self.assertEqual(extract_youtube_id("https://m.youtube.com/watch?v=mob123"), "mob123")

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
            "youtube_url": "https://youtu.be/big123",
            "vk_url": "https://vk.com/video-1_2",
            "author_name": "Прокудин",
            "author_username": "ny_pochemu",
            "montage_name": "Max",
            "montage_username": "max",
            "added_by_username": "lead",
            "added_by_tg_id": 1,
        }
        bigrecap = format_video_card({**base, "video_type": "bigrecap"})
        regular = format_video_card({**base, "video_type": "regular", "instagram_url": "https://www.instagram.com/reel/ABC/"})
        self.assertIn("Тип: большой рекап", bigrecap)
        self.assertIn("YouTube: https://youtu.be/big123", bigrecap)
        self.assertIn("VK: https://vk.com/video-1_2", bigrecap)
        self.assertNotIn("Instagram:", bigrecap)
        self.assertNotIn("TikTok:", bigrecap)
        self.assertNotIn("Тип:", regular)

    def test_video_sheet_row_includes_video_type_after_status(self) -> None:
        row = video_to_row(
            {
                "id": 10,
                "status": "pending",
                "video_type": "bigrecap",
                "youtube_id": "big123",
                "author_name": "Прокудин",
                "author_username": "ny_pochemu",
            }
        )
        self.assertEqual(SHEET_COLUMNS[:3], ["id", "status", "video_type"])
        self.assertEqual(row[:3], ["10", "pending", "bigrecap"])
        self.assertEqual(row[SHEET_COLUMNS.index("youtube_id")], "big123")

    def test_start_new_video_stores_regular_instagram_first_flow(self) -> None:
        tg = FakeTelegram()
        actor = Actor(tg_id=1, chat_id=1, username="user")
        with patch("bot.handlers.db.set_session") as set_session:
            start_new_video(tg, actor)
        self.assertEqual(set_session.call_args.kwargs["state"], "new:instagram")
        self.assertEqual(
            set_session.call_args.kwargs["data"],
            {"video_type": "regular", "platform_flow": "instagram_first"},
        )
        self.assertIn("Instagram/Reels", tg.messages[0]["text"])

    def test_start_new_bigrecap_stores_type_in_session(self) -> None:
        tg = FakeTelegram()
        actor = Actor(tg_id=1, chat_id=1, username="user")
        with patch("bot.handlers.db.set_session") as set_session:
            start_new_bigrecap(tg, actor)
        self.assertEqual(set_session.call_args.kwargs["state"], "new:bigrecap_youtube")
        self.assertEqual(
            set_session.call_args.kwargs["data"],
            {
                "video_type": "bigrecap",
                "platform_flow": "youtube_vk",
                "step": "awaiting_bigrecap_youtube",
            },
        )
        self.assertEqual(tg.messages[0]["text"], BIGRECAP_YOUTUBE_PROMPT)

    def test_bigrecap_invalid_youtube_keeps_user_on_youtube_step(self) -> None:
        tg = FakeTelegram()
        actor = Actor(tg_id=1, chat_id=1, username="user")
        with patch("bot.handlers.db.set_session") as set_session:
            handle_new_bigrecap_youtube(tg, actor, {"video_type": "bigrecap"}, "https://instagram.com/reel/ABC/")
        set_session.assert_not_called()
        self.assertEqual(tg.messages[0]["text"], BIGRECAP_YOUTUBE_INVALID_MESSAGE)

    def test_bigrecap_youtube_step_sets_people_flow_without_instagram(self) -> None:
        tg = FakeTelegram()
        actor = Actor(tg_id=1, chat_id=1, username="user")
        with (
            patch("bot.handlers.find_video_by_youtube_id", return_value=None),
            patch("bot.handlers.db.set_session") as set_session,
            patch("bot.handlers.ask_people") as ask_people_mock,
        ):
            handle_new_bigrecap_youtube(tg, actor, {"video_type": "bigrecap"}, "https://youtu.be/big123")
        data = set_session.call_args.kwargs["data"]
        self.assertEqual(set_session.call_args.kwargs["state"], "new:author")
        self.assertEqual(data["youtube_id"], "big123")
        self.assertIsNone(data["instagram_url"])
        self.assertIsNone(data["instagram_id"])
        self.assertIsNone(data["tiktok_url"])
        self.assertIsNone(data["tiktok_id"])
        ask_people_mock.assert_called_once_with(tg, actor, "author")

    def test_bigrecap_submission_data_clears_instagram_and_tiktok(self) -> None:
        data = normalized_submission_data(
            {
                "video_type": "bigrecap",
                "instagram_url": "https://www.instagram.com/reel/OLD/",
                "instagram_id": "OLD",
                "youtube_url": "https://youtu.be/big123",
                "youtube_id": "big123",
                "tiktok_url": "https://www.tiktok.com/@x/video/1",
                "tiktok_id": "1",
            }
        )
        self.assertEqual(data["video_type"], "bigrecap")
        self.assertEqual(data["youtube_id"], "big123")
        self.assertIsNone(data["instagram_url"])
        self.assertIsNone(data["instagram_id"])
        self.assertIsNone(data["tiktok_url"])
        self.assertIsNone(data["tiktok_id"])

    def test_bigrecap_after_montage_asks_vk_not_tiktok(self) -> None:
        tg = FakeTelegram()
        actor = Actor(tg_id=1, chat_id=1, username="user")
        data = {
            "video_type": "bigrecap",
            "youtube_url": "https://youtu.be/big123",
            "youtube_id": "big123",
        }
        with patch("bot.handlers.db.set_session") as set_session:
            next_after_person(tg, actor, "montage", data)
        self.assertEqual(set_session.call_args.kwargs["state"], "new:bigrecap_vk_choice")
        self.assertEqual(tg.messages[0]["text"], "Добавить ссылку VK?")
        keyboard = tg.messages[0]["reply_markup"]["inline_keyboard"]  # type: ignore[index]
        self.assertEqual(keyboard[0][0]["text"], "Добавить VK")
        self.assertEqual(keyboard[1][0]["text"], "Пропустить VK")
        self.assertNotIn("TikTok", tg.messages[0]["text"])

    def test_voice_prompt_uses_new_wording_and_no_button(self) -> None:
        tg = FakeTelegram()
        actor = Actor(tg_id=1, chat_id=1, username="user")
        with patch("bot.handlers.get_people", return_value=[]):
            ask_people(tg, actor, "voice")
        message = tg.messages[0]
        self.assertEqual(message["text"], "Была ли в ролике озвучка другого автора?")
        keyboard = message["reply_markup"]["inline_keyboard"]  # type: ignore[index]
        self.assertEqual(keyboard[0][0]["text"], "Да, была")
        self.assertEqual(keyboard[0][1]["text"], "Нет, не было")


class BotV1010Tests(unittest.TestCase):
    def test_add_znambo_rejects_non_superadmin_with_exact_message(self) -> None:
        tg = FakeTelegram()
        actor = Actor(tg_id=10, chat_id=10, username="user")
        with (
            patch("bot.handlers.is_superadmin", return_value=False),
            patch("bot.handlers.db.set_session") as set_session,
        ):
            start_add_znambo(tg, actor)
        set_session.assert_not_called()
        self.assertEqual(tg.messages[0]["text"], ADD_ZNAMBO_UNAUTHORIZED_MESSAGE)

    def test_add_znambo_starts_private_superadmin_session(self) -> None:
        tg = FakeTelegram()
        actor = Actor(tg_id=1, chat_id=1, username="znambo")
        with (
            patch("bot.handlers.is_superadmin", return_value=True),
            patch("bot.handlers.db.set_session") as set_session,
        ):
            start_add_znambo(tg, actor)
        self.assertEqual(set_session.call_args.kwargs["state"], ADD_ZNAMBO_SESSION_INSTAGRAM)
        self.assertEqual(
            set_session.call_args.kwargs["data"],
            {"flow": "add_znambo", "step": "awaiting_znambo_instagram", "video_type": "regular"},
        )
        self.assertEqual(tg.messages[0]["text"], ADD_ZNAMBO_LINK_PROMPT)

    def test_add_znambo_invalid_instagram_keeps_session_active(self) -> None:
        tg = FakeTelegram()
        actor = Actor(tg_id=1, chat_id=1, username="znambo")
        with (
            patch("bot.handlers.is_superadmin", return_value=True),
            patch("bot.handlers.db.clear_session") as clear_session,
        ):
            handle_add_znambo_instagram(tg, actor, {"flow": "add_znambo"}, "https://youtu.be/not-instagram")
        clear_session.assert_not_called()
        self.assertEqual(tg.messages[0]["text"], ADD_ZNAMBO_INVALID_LINK_MESSAGE)

    def test_add_znambo_instagram_asks_publish_date(self) -> None:
        tg = FakeTelegram()
        actor = Actor(tg_id=1, chat_id=1, username="znambo")
        with (
            patch("bot.handlers.is_superadmin", return_value=True),
            patch("bot.handlers.find_video_by_instagram_id", return_value=None),
            patch("bot.handlers.db.set_session") as set_session,
        ):
            handle_add_znambo_instagram(tg, actor, {"flow": "add_znambo"}, "instagram.com/reel/ABC123/?utm_source=x")
        self.assertEqual(set_session.call_args.kwargs["state"], ADD_ZNAMBO_SESSION_DATE)
        data = set_session.call_args.kwargs["data"]
        self.assertEqual(data["step"], "awaiting_znambo_publish_date")
        self.assertEqual(data["instagram_id"], "ABC123")
        self.assertEqual(tg.messages[0]["text"], ADD_ZNAMBO_DATE_PROMPT)
        keyboard = tg.messages[0]["reply_markup"]["inline_keyboard"]  # type: ignore[index]
        self.assertEqual(
            [[button["text"] for button in row] for row in keyboard],
            [["Сегодня", "Вчера"], ["Ввести вручную"]],
        )
        self.assertEqual(keyboard[1][0]["callback_data"], "znambo:date:manual")

    def test_add_znambo_manual_callback_bypasses_preset_parser(self) -> None:
        callback = {
            "id": "callback-1",
            "data": "znambo:date:manual",
            "from": {"id": 1, "username": "znambo"},
            "message": {"message_id": 10, "chat": {"id": 1, "type": "private"}},
        }
        with (
            patch("bot.handlers.TelegramClient"),
            patch("bot.handlers.start_add_znambo_manual_date") as start_manual,
            patch("bot.handlers.handle_add_znambo_date") as handle_date,
        ):
            handle_callback(callback)
        start_manual.assert_called_once()
        handle_date.assert_not_called()

    def test_add_znambo_manual_prompt_keeps_date_session(self) -> None:
        tg = FakeTelegram()
        actor = Actor(tg_id=1, chat_id=1, username="znambo")
        session = {"state": ADD_ZNAMBO_SESSION_DATE, "data": {"flow": "add_znambo"}}
        with (
            patch("bot.handlers.is_superadmin", return_value=True),
            patch("bot.handlers.db.get_session", return_value=session),
            patch("bot.handlers.db.clear_session") as clear_session,
        ):
            start_add_znambo_manual_date(tg, actor)
        clear_session.assert_not_called()
        self.assertEqual(tg.messages[0]["text"], ADD_ZNAMBO_MANUAL_DATE_PROMPT)

    def test_add_znambo_invalid_manual_date_keeps_session_active(self) -> None:
        tg = FakeTelegram()
        actor = Actor(tg_id=1, chat_id=1, username="znambo")
        session = {"state": ADD_ZNAMBO_SESSION_DATE, "data": {"flow": "add_znambo"}}
        with (
            patch("bot.handlers.is_superadmin", return_value=True),
            patch("bot.handlers.db.get_session", return_value=session),
            patch("bot.handlers.db.clear_session") as clear_session,
            patch("bot.handlers.upsert_znambo_quick_video") as upsert,
        ):
            handle_add_znambo_date(tg, actor, "32.07")
        clear_session.assert_not_called()
        upsert.assert_not_called()
        self.assertEqual(tg.messages[0]["text"], ADD_ZNAMBO_INVALID_DATE_MESSAGE)

    def test_add_znambo_dates_approve_and_sync_sheets(self) -> None:
        actor = Actor(tg_id=1, chat_id=1, username="znambo")
        today = datetime.now(get_settings().tz).date()
        cases = {
            "Сегодня": today,
            "Вчера": today - timedelta(days=1),
            "12.07": date(today.year, 7, 12),
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                tg = FakeTelegram()
                data = {"flow": "add_znambo", "instagram_id": "ABC123"}
                session = {"state": ADD_ZNAMBO_SESSION_DATE, "data": data}
                video = {
                    "id": 77,
                    "publish_date": expected,
                    "instagram_url": "https://www.instagram.com/reel/ABC123/",
                    "status": "approved",
                }
                with (
                    patch("bot.handlers.is_superadmin", return_value=True),
                    patch("bot.handlers.db.get_session", return_value=session),
                    patch("bot.handlers.db.clear_session") as clear_session,
                    patch("bot.handlers.upsert_znambo_quick_video", return_value={"video": video}) as upsert,
                    patch("bot.handlers.sync_znambo_quick_to_sheets", return_value=(True, None)) as sync,
                ):
                    handle_add_znambo_date(tg, actor, raw)
                self.assertEqual(upsert.call_args.args[2], expected)
                clear_session.assert_called_once_with(actor.tg_id)
                sync.assert_called_once_with(video, actor)
                self.assertIn("✅ Ролик Знамбо добавлен", tg.messages[0]["text"])

    def test_add_znambo_active_duplicate_clears_session_and_shows_details(self) -> None:
        tg = FakeTelegram()
        actor = Actor(tg_id=1, chat_id=1, username="znambo")
        duplicate = {"id": 77, "status": "approved", "publish_date": "2026-07-16"}
        with (
            patch("bot.handlers.is_superadmin", return_value=True),
            patch("bot.handlers.find_video_by_instagram_id", return_value=duplicate),
            patch("bot.handlers.db.clear_session") as clear_session,
        ):
            handle_add_znambo_instagram(tg, actor, {"flow": "add_znambo"}, "instagram.com/reel/ABC123/")
        clear_session.assert_called_once_with(actor.tg_id)
        text = tg.messages[0]["text"]
        self.assertIn("Этот ролик уже есть в базе.", text)
        self.assertIn("ID: 77", text)
        self.assertIn("Статус: approved", text)
        self.assertIn("Дата: 16.07.2026", text)

    def test_add_znambo_date_parser_accepts_presets_and_formats(self) -> None:
        self.assertEqual(parse_add_znambo_date("2026-07-16").isoformat(), "2026-07-16")
        dd_mm = parse_add_znambo_date("16.07")
        self.assertEqual((dd_mm.day, dd_mm.month), (16, 7))
        self.assertEqual((parse_add_znambo_date("Сегодня") - parse_add_znambo_date("Вчера")).days, 1)
        self.assertEqual((parse_add_znambo_date("Вчера") - parse_add_znambo_date("Позавчера")).days, 1)
        with self.assertRaisesRegex(ValueError, ADD_ZNAMBO_INVALID_DATE_MESSAGE):
            parse_add_znambo_date("tomorrow")

    def test_add_znambo_success_card_has_no_empty_extra_platform_lines(self) -> None:
        text = format_add_znambo_success(
            {
                "publish_date": "2026-07-16",
                "instagram_url": "https://www.instagram.com/reel/ABC123/",
                "status": "approved",
            }
        )
        self.assertIn("✅ Ролик Знамбо добавлен", text)
        self.assertIn("Дата: 16.07.2026", text)
        self.assertIn("Автор: Знамбо (@znambo)", text)
        self.assertIn("Озвучка: Знамбо (@znambo)", text)
        self.assertIn("Монтажёр: Знамбо (@znambo)", text)
        self.assertIn("Статус: approved", text)
        self.assertNotIn("YouTube:", text)
        self.assertNotIn("TikTok:", text)
        self.assertNotIn("VK:", text)

    def test_add_znambo_button_is_superadmin_only(self) -> None:
        tg = FakeTelegram()
        actor = Actor(tg_id=1, chat_id=1, username="znambo")
        with (
            patch("bot.handlers.is_superadmin", return_value=True),
            patch("bot.handlers.is_admin", return_value=False),
        ):
            _send_main_menu(tg, actor, "menu")
        keyboard = tg.messages[0]["reply_markup"]["inline_keyboard"]  # type: ignore[index]
        texts = [button["text"] for row in keyboard for button in row]
        self.assertIn("⚡ Добавить мой ролик", texts)

        tg = FakeTelegram()
        with (
            patch("bot.handlers.is_superadmin", return_value=False),
            patch("bot.handlers.is_admin", return_value=False),
        ):
            _send_main_menu(tg, actor, "menu")
        keyboard = tg.messages[0]["reply_markup"]["inline_keyboard"]  # type: ignore[index]
        texts = [button["text"] for row in keyboard for button in row]
        self.assertNotIn("⚡ Добавить мой ролик", texts)


if __name__ == "__main__":
    unittest.main()
