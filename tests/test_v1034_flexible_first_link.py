from __future__ import annotations

import unittest

from bot import cancel_command, flexible_first_link as flow


class FlexibleFirstLinkV1034Tests(unittest.TestCase):
    def test_cancel_command_accepts_bot_suffix(self) -> None:
        self.assertTrue(cancel_command.is_cancel_message({"text": "/cancel"}))
        self.assertTrue(cancel_command.is_cancel_message({"text": "/cancel@rngn_reels_wc_bot"}))
        self.assertFalse(cancel_command.is_cancel_message({"text": "/new_video"}))

    def test_youtube_short_can_be_first_link(self) -> None:
        platform, link = flow.parse_first_link(
            "https://www.youtube.com/shorts/GceyVMGmggs"
        )
        self.assertEqual(platform, "youtube")
        self.assertEqual(link.external_id, "GceyVMGmggs")

    def test_first_platform_is_not_requested_twice(self) -> None:
        data = {
            "youtube_url": "https://www.youtube.com/shorts/GceyVMGmggs",
            "youtube_id": "GceyVMGmggs",
        }
        self.assertEqual(flow._next_missing_platform(data), "instagram")
        data["instagram_url"] = "https://www.instagram.com/reel/example/"
        self.assertEqual(flow._next_missing_platform(data), "tiktok")

    def test_skipped_platform_is_not_requested_again(self) -> None:
        data = {
            "instagram_url": "https://www.instagram.com/reel/example/",
            "skipped_platforms": ["youtube"],
        }
        self.assertEqual(flow._next_missing_platform(data), "tiktok")


if __name__ == "__main__":
    unittest.main()
