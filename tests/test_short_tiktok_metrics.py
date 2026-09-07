from __future__ import annotations

import requests

from bot import multiplatform_metrics, short_tiktok_metrics


class _Response:
    def __init__(
        self,
        text: str = "",
        payload=None,
        error: Exception | None = None,
        url: str = "",
    ):
        self.text = text
        self._payload = payload
        self._error = error
        self.url = url

    def raise_for_status(self) -> None:
        if self._error:
            raise self._error

    def json(self):
        return self._payload

    def close(self) -> None:
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


def test_short_tiktok_resolves_exact_official_redirect(monkeypatch) -> None:
    canonical = "https://www.tiktok.com/@got_ball/video/7680000000000000001?is_from_webapp=1"
    monkeypatch.setattr(
        short_tiktok_metrics.requests,
        "get",
        lambda *args, **kwargs: _Response(url=canonical),
    )
    monkeypatch.setattr(short_tiktok_metrics, "_persist_resolved", lambda resolved: len(resolved))
    try:
        result = short_tiktok_metrics.refresh([_video()])
        assert result == {
            "short_links": 1,
            "resolved": 1,
            "persisted": 1,
            "direct_resolved": 1,
            "core_resolved": 0,
            "ambiguous": 0,
            "unmatched": 0,
            "core_lookup_failed": 0,
            "source": "redirect",
        }
        assert multiplatform_metrics._platform_id(_video(), "tiktok") == "7680000000000000001"
    finally:
        _cleanup()


def test_short_tiktok_rejects_redirect_off_tiktok(monkeypatch) -> None:
    monkeypatch.setattr(
        short_tiktok_metrics.requests,
        "get",
        lambda *args, **kwargs: _Response(url="https://example.com/video/7680000000000000001"),
    )
    assert short_tiktok_metrics._resolve_short_url(_video()["tiktok_url"]) == ""


def test_short_tiktok_resolves_from_confirmed_core_group(monkeypatch) -> None:
    tsv = "\t".join(["Instagram URL", "TikTok URL", "YouTube URL", "VK URL"]) + "\n"
    tsv += "\t".join([
        "https://www.instagram.com/reel/DbnyJvqNlig/",
        "https://www.tiktok.com/@got_ball/video/7680000000000000001",
        "https://youtu.be/624ToNvFr2w",
        "https://vk.ru/clip-202211208_456262347",
    ]) + "\n"
    monkeypatch.setattr(short_tiktok_metrics, "_resolve_short_url", lambda url: "")
    monkeypatch.setattr(short_tiktok_metrics.requests, "get", lambda *args, **kwargs: _Response(text=tsv))
    monkeypatch.setattr(short_tiktok_metrics, "_persist_resolved", lambda resolved: len(resolved))
    try:
        result = short_tiktok_metrics.refresh([_video()])
        assert result["resolved"] == 1
        assert result["direct_resolved"] == 0
        assert result["core_resolved"] == 1
        assert result["source"] == "mirror"
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
    monkeypatch.setattr(short_tiktok_metrics, "_resolve_short_url", lambda url: "")
    monkeypatch.setattr(short_tiktok_metrics.requests, "get", lambda *args, **kwargs: _Response(text=header + row1 + row2))
    monkeypatch.setattr(short_tiktok_metrics, "_persist_resolved", lambda resolved: len(resolved))
    try:
        result = short_tiktok_metrics.refresh([_video()])
        assert result["resolved"] == 0
        assert result["ambiguous"] == 1
        assert multiplatform_metrics._platform_id(_video(), "tiktok").startswith("short:")
    finally:
        _cleanup()


def test_short_tiktok_falls_back_to_dashboard_when_mirror_is_unavailable(monkeypatch) -> None:
    dashboard_row = {
        "Instagram URL": "https://www.instagram.com/reel/DbnyJvqNlig/",
        "TikTok URL": "https://www.tiktok.com/@got_ball/video/7680000000000000001",
        "YouTube URL": "https://youtu.be/624ToNvFr2w",
        "VK URL": "https://vk.ru/clip-202211208_456262347",
    }
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        if len(calls) == 1:
            return _Response(error=requests.HTTPError("503 Service Unavailable"))
        return _Response(payload={"ok": True, "rows": [dashboard_row]})

    monkeypatch.setattr(short_tiktok_metrics, "_resolve_short_url", lambda url: "")
    monkeypatch.setattr(short_tiktok_metrics.requests, "get", fake_get)
    monkeypatch.setattr(short_tiktok_metrics, "_persist_resolved", lambda resolved: len(resolved))
    try:
        result = short_tiktok_metrics.refresh([_video()])
        assert result["resolved"] == 1
        assert result["core_resolved"] == 1
        assert result["source"] == "dashboard"
        assert calls[0].endswith("/videos-v2.tsv")
        assert calls[1] == short_tiktok_metrics.DASHBOARD_VIDEOS_URL
    finally:
        _cleanup()
