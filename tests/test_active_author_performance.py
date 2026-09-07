from __future__ import annotations

from datetime import datetime, timezone

from bot import active_author_performance


def _video(video_id: int, publish_date: str, author: str, username: str) -> dict:
    return {
        "id": video_id,
        "status": "approved",
        "video_type": "regular",
        "project_code": "vzyal_myach",
        "project_name": "Взял Мяч",
        "publish_date": publish_date,
        "author_name": author,
        "author_username": username,
        "instagram_id": f"ig-{video_id}",
        "instagram_url": f"https://instagram.com/reel/{video_id}",
        "youtube_id": f"yt{video_id:09d}"[:11],
        "youtube_url": f"https://youtu.be/yt{video_id:09d}"[:28],
    }


def test_build_rows_counts_total_and_best_video() -> None:
    videos = [
        _video(1, "2026-08-01", "Автор А", "author_a"),
        _video(2, "2026-08-02", "Автор А", "author_a"),
        _video(3, "2026-08-03", "Автор Б", "author_b"),
        {
            **_video(4, "2026-07-01", "Архив", "archive"),
            "project_code": "world_cup_2026",
        },
    ]
    captured = datetime(2026, 9, 7, tzinfo=timezone.utc)
    latest = {
        (1, "instagram"): {"views": 100, "captured_at": captured},
        (1, "youtube"): {"views": 50, "captured_at": captured},
        (2, "instagram"): {"views": 300, "captured_at": captured},
        (2, "youtube"): {"views": 25, "captured_at": captured},
        (3, "instagram"): {"views": 200, "captured_at": captured},
    }

    rows = active_author_performance.build_rows(videos, latest)

    assert len(rows) == 2
    author_a = next(row for row in rows if row[1] == "Автор А")
    author_b = next(row for row in rows if row[1] == "Автор Б")

    assert author_a[3] == 2
    assert author_a[4] == 475
    assert author_a[5] == 325
    assert author_a[6] == 2
    assert author_a[7] == "2026-08-02"

    assert author_b[3] == 1
    assert author_b[4] == 200
    assert author_b[5] == 200
    assert author_b[6] == 3


def test_rows_sorted_by_total_views_descending() -> None:
    videos = [
        _video(1, "2026-08-01", "Автор А", "author_a"),
        _video(2, "2026-08-02", "Автор Б", "author_b"),
    ]
    captured = datetime(2026, 9, 7, tzinfo=timezone.utc)
    latest = {
        (1, "instagram"): {"views": 10, "captured_at": captured},
        (2, "instagram"): {"views": 20, "captured_at": captured},
    }

    rows = active_author_performance.build_rows(videos, latest)

    assert [row[1] for row in rows] == ["Автор Б", "Автор А"]
