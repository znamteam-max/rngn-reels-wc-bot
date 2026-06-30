from __future__ import annotations

import unittest

from bot.links import (
    normalize_instagram,
    normalize_tiktok,
    normalize_vk,
    normalize_youtube,
    parse_publish_date,
)


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


if __name__ == "__main__":
    unittest.main()

