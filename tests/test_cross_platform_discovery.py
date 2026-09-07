from __future__ import annotations

from datetime import date

from bot import cross_platform_discovery


def _video() -> dict:
    return {
        "id": 360,
        "project_code": "vzyal_myach",
        "project_name": "Взял Мяч",
        "publish_date": date(2026, 8, 13),
        "instagram_url": "https://www.instagram.com/reel/Db_jBYpNTP6/",
        "instagram_id": "Db_jBYpNTP6",
        "youtube_url": "",
        "youtube_id": "",
        "tiktok_url": "",
        "tiktok_id": "",
        "vk_url": "",
    }


def _source() -> dict[str, str]:
    return {
        "publication_id": "ig-1",
        "project": "Взял Мяч",
        "platform": "instagram",
        "external_post_id": "Db_jBYpNTP6",
        "url": "https://www.instagram.com/reel/Db_jBYpNTP6/",
        "caption": "Леброн объяснил, почему этот бросок изменил всю игру",
        "published_at": "2026-08-13T12:00:00Z",
    }


def test_matches_similar_cross_platform_caption_in_same_project_family() -> None:
    youtube = {
        "publication_id": "yt-1",
        "project": "Взял Мяч Клипы",
        "platform": "youtube",
        "external_post_id": "5zWisAYEfto",
        "url": "https://youtu.be/5zWisAYEfto",
        "caption": "Леброн объяснил почему этот бросок изменил всю игру",
        "published_at": "2026-08-13T13:00:00Z",
    }
    candidate, status = cross_platform_discovery._choose_candidate(
        platform="youtube",
        video=_video(),
        source_rows=[_source()],
        all_rows=[_source(), youtube],
    )
    assert status == "matched"
    assert candidate == youtube


def test_close_competing_candidates_are_left_ambiguous() -> None:
    row1 = {
        "publication_id": "yt-1",
        "project": "Взял Мяч",
        "platform": "youtube",
        "external_post_id": "5zWisAYEfto",
        "url": "https://youtu.be/5zWisAYEfto",
        "caption": "Леброн объяснил почему этот бросок изменил всю игру",
        "published_at": "2026-08-13T11:00:00Z",
    }
    row2 = {
        "publication_id": "yt-2",
        "project": "Взял Мяч Клипы",
        "platform": "youtube",
        "external_post_id": "TFYdSaLmcgg",
        "url": "https://youtu.be/TFYdSaLmcgg",
        "caption": "Леброн объяснил почему этот бросок изменил всю игру",
        "published_at": "2026-08-13T14:00:00Z",
    }
    candidate, status = cross_platform_discovery._choose_candidate(
        platform="youtube",
        video=_video(),
        source_rows=[_source()],
        all_rows=[_source(), row1, row2],
    )
    assert candidate is None
    assert status == "ambiguous"


def test_vk_wall_url_is_not_promoted_to_clip_identity() -> None:
    row = {
        "project": "Взял Мяч",
        "platform": "vk",
        "external_post_id": "-202211208_12345",
        "url": "https://vk.ru/wall-202211208_12345",
        "caption": "Леброн объяснил почему этот бросок изменил всю игру",
        "published_at": "2026-08-13T13:00:00Z",
    }
    assert cross_platform_discovery._candidate_safe_url("vk", row) is None
