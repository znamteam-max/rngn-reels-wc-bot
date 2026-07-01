from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from bot.handlers import Actor, sync_youtube_metrics_command
from bot.links import normalize_youtube
from bot.metrics import (
    YouTubeSyncResult,
    latest_snapshot_per_video,
    summarize_metric_rows,
    top_metric_rows,
)
from bot.youtube_metrics import parse_youtube_statistics_response


class FakeTelegram:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []

    def send_message(self, chat_id: int, text: str, reply_markup=None, disable_web_page_preview=True):
        self.messages.append((chat_id, text))
        return {"ok": True, "result": {"message_id": len(self.messages)}}


class YouTubeMetricsTests(unittest.TestCase):
    def test_youtube_id_extraction_forms(self) -> None:
        cases = {
            "https://youtu.be/-ZWRg5DvQS0": "-ZWRg5DvQS0",
            "https://www.youtube.com/watch?v=-ZWRg5DvQS0": "-ZWRg5DvQS0",
            "https://youtube.com/watch?v=-ZWRg5DvQS0&utm_source=x": "-ZWRg5DvQS0",
            "https://www.youtube.com/shorts/-ZWRg5DvQS0": "-ZWRg5DvQS0",
            "https://youtube.com/shorts/-ZWRg5DvQS0?si=tracking": "-ZWRg5DvQS0",
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                link = normalize_youtube(url)
                self.assertEqual(link.external_id, expected)
                self.assertEqual(link.url, f"https://youtu.be/{expected}")

    def test_youtube_invalid_url_has_no_external_id(self) -> None:
        link = normalize_youtube("https://example.com/watch?v=-ZWRg5DvQS0")
        self.assertIsNone(link.external_id)

    def test_youtube_api_response_parsing(self) -> None:
        stats_by_id, missing = parse_youtube_statistics_response(
            ["a", "b", "c"],
            {
                "items": [
                    {
                        "id": "a",
                        "statistics": {
                            "viewCount": "100",
                            "likeCount": "7",
                            "commentCount": "2",
                        },
                    },
                    {
                        "id": "b",
                        "statistics": {
                            "viewCount": "50",
                        },
                    },
                ]
            },
        )
        self.assertEqual(stats_by_id["a"].views, 100)
        self.assertEqual(stats_by_id["a"].likes, 7)
        self.assertEqual(stats_by_id["a"].comments, 2)
        self.assertEqual(stats_by_id["b"].views, 50)
        self.assertIsNone(stats_by_id["b"].likes)
        self.assertIsNone(stats_by_id["b"].comments)
        self.assertEqual(missing, ["c"])

    def test_summary_calculations(self) -> None:
        rows = [
            {"video_id": 1, "views": 10, "likes": 2, "comments": 1},
            {"video_id": 2, "views": 30, "likes": 3, "comments": None},
            {"video_id": 3, "views": None, "likes": None, "comments": 5},
        ]
        totals = summarize_metric_rows(rows)
        self.assertEqual(totals["views"], 40)
        self.assertEqual(totals["likes"], 5)
        self.assertEqual(totals["comments"], 6)
        self.assertEqual(top_metric_rows(rows, 1)[0]["video_id"], 2)

    def test_latest_snapshot_per_video(self) -> None:
        older = datetime(2026, 7, 1, 8, tzinfo=timezone.utc)
        newer = datetime(2026, 7, 1, 9, tzinfo=timezone.utc)
        rows = [
            {"video_id": 1, "captured_at": older, "views": 10},
            {"video_id": 1, "captured_at": newer, "views": 15},
            {"video_id": 2, "captured_at": older, "views": 7},
        ]
        latest = {row["video_id"]: row for row in latest_snapshot_per_video(rows)}
        self.assertEqual(latest[1]["views"], 15)
        self.assertEqual(latest[2]["views"], 7)

    def test_sync_youtube_metrics_non_admin_denied(self) -> None:
        tg = FakeTelegram()
        actor = Actor(tg_id=10, chat_id=10, username="user")
        with patch("bot.handlers.is_admin", return_value=False):
            sync_youtube_metrics_command(tg, actor)
        self.assertIn("только админам", tg.messages[0][1])

    def test_sync_youtube_metrics_admin_runs(self) -> None:
        tg = FakeTelegram()
        actor = Actor(tg_id=10, chat_id=10, username="admin")
        result = YouTubeSyncResult(
            total_videos=1,
            success_count=1,
            total_views=100,
            total_likes=5,
            total_comments=2,
            top_video_id=1,
            top_views=100,
            sheet_status="ok",
        )
        with patch("bot.handlers.is_admin", return_value=True), patch(
            "bot.handlers.metrics.sync_youtube_metrics",
            return_value=result,
        ) as sync_mock:
            sync_youtube_metrics_command(tg, actor)
        sync_mock.assert_called_once()
        self.assertIn("YouTube-метрики обновлены", tg.messages[0][1])


if __name__ == "__main__":
    unittest.main()
