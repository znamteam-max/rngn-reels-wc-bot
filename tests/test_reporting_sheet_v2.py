from __future__ import annotations

from datetime import date

from bot import reporting_sheet_v2 as report


def _video(video_id: int, project: str, published: str, *, comment: str = "", status: str = "approved"):
    return {
        "id": video_id,
        "status": status,
        "video_type": "regular",
        "project_code": project,
        "project_name": "Взял Мяч" if project == "vzyal_myach" else "ЧМ 2026",
        "publish_date": published,
        "comment": comment,
        "author_name": "Тихонов",
        "author_username": "tikhonov32",
        "montage_name": "Рябошапка",
        "montage_username": "igormoll",
        "voice_name": "",
        "voice_username": "",
        "instagram_url": f"https://instagram.com/reel/{video_id}/",
        "instagram_id": str(video_id),
        "youtube_url": "",
        "youtube_id": "",
        "tiktok_url": "",
        "vk_url": "",
    }


def test_active_reporting_excludes_world_cup_and_sorts_newest_first():
    videos = [
        _video(1, "world_cup_2026", "2026-07-20"),
        _video(2, "vzyal_myach", "2026-08-04"),
        _video(3, "vzyal_myach", "2026-08-26"),
        _video(4, "vzyal_myach", "2026-08-30", status="pending"),
    ]
    assert [item["id"] for item in report.active_reporting_videos(videos)] == [3, 2]


def test_aircut_is_human_readable_work_type():
    video = _video(351, "vzyal_myach", "2026-08-04", comment="Отрез из эфира")
    assert report._work_type(video) == "Отрез из эфира"


def test_report_expands_one_work_into_role_rows(monkeypatch):
    video = _video(351, "vzyal_myach", "2026-08-04")
    latest = {
        (351, "instagram"): {
            "views": 123,
            "captured_at": None,
        }
    }
    rows = report.build_report_rows([video], latest)
    assert len(rows) == 2
    assert {row[4] for row in rows} == {"Автор", "Монтаж"}
    assert all(row[0] == "2026-08-04" for row in rows)
    assert all(row[1] == "Взял Мяч" for row in rows)


def test_summary_has_month_not_all_and_counts_aircut():
    video = _video(351, "vzyal_myach", "2026-08-04", comment="Отрез из эфира")
    latest = {(351, "instagram"): {"views": 123, "captured_at": None}}
    rows = report.build_summary_rows([video], latest)
    assert rows[0][0] == "2026-08"
    assert rows[0][3] == 1
    assert rows[0][5] == 1
    assert "ALL" not in {row[0] for row in rows}
