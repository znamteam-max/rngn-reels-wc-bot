from __future__ import annotations

from bot import multiplatform_metrics, short_tiktok_metrics


class _Response:
    def __init__(self, text: str):
        self.text = text

    def raise_for_status(self) -> None:
        return None


def _video() -> dict:
    return {
        "id": 351,
        "instagram_id": "DbnyJvqNlig",
        "instagram_url": "https://www.instagram.com/reel/DbnyJvqNlig/",
        "youtube_id": "624ToNvFr2w",
        "youtube_url": "https://youtu.be/624ToNvFr2w",
        "tiktok_url": "https://www.tiktok.com/t/ZTUMB1Njt/",
        "vk_url": "https://vk.ru/clip-202211208_456262347?c=1",
    }


def _cleanup() -> None:
    multiplatform_metrics._platform_id = short_tiktok_metrics._ORIGINAL_PLATFORM_ID
    short_tiktok_metrics._INSTALLED = False
    short_tiktok_metrics._CANONICAL_TIKTOK_BY_VIDEO_ID.clear()


def test_short_tiktok_resolves_from_confirmed_core_group(monkeypatch) -> None:
    tsv = "\t".join(["Instagram URL", "TikTok URL", "YouTube URL", "VK URL"]) + "\n"
    tsv += "\t".join([
        "https://www.instagram.com/reel/DbnyJvqNlig/",
        "https://www.tiktok.com/@got_ball/video/7680000000000000001",
        "https://youtu.be/624ToNvFr2w",
        "https://vk.ru/clip-202211208_456262347",
    ]) + "\n"
    monkeypatch.setattr(short_tiktok_metrics.requests, "get", lambda *args, **kwargs: _Response(tsv))
    monkeypatch.setattr(short_tiktok_metrics, "_persist_resolved", lambda resolved: len(resolved))
    try:
        result = short_tiktok_metrics.refresh([_video()])
        assert result == {
            "short_links": 1,
            "resolved": 1,
            "persisted": 1,
            "ambiguous": 0,
            "unmatched": 0,
        }
        assert multiplatform_metrics._platform_id(_video(), "tiktok") == "7680000000000000001"
    finally:
        _cleanup()


def test_short_tiktok_ambiguity_is_not_auto_attached(monkeypatch) -> None:
    header = "\t".join(["Instagram URL", "TikTok URL", "YouTube URL", "VK URL"]) + "\n"
    row1 = "\t".join([
        "https://www.instagram.com/reel/DbnyJvqNlig/",
        "https://www.tiktok.com/@got_ball/video/7680000000000000001",
        "",
        "",
    ]) + "\n"
    row2 = "\t".join([
        "https://www.instagram.com/reel/DbnyJvqNlig/",
        "https://www.tiktok.com/@got_ball/video/7680000000000000002",
        "",
        "",
    ]) + "\n"
    monkeypatch.setattr(short_tiktok_metrics.requests, "get", lambda *args, **kwargs: _Response(header + row1 + row2))
    monkeypatch.setattr(short_tiktok_metrics, "_persist_resolved", lambda resolved: len(resolved))
    try:
        result = short_tiktok_metrics.refresh([_video()])
        assert result == {
            "short_links": 1,
            "resolved": 0,
            "persisted": 0,
            "ambiguous": 1,
            "unmatched": 0,
        }
        assert multiplatform_metrics._platform_id(_video(), "tiktok").startswith("short:")
    finally:
        _cleanup()
