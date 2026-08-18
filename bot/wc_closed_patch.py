from __future__ import annotations

from typing import Any

from bot import db, reconciliation

WORLD_CUP_CODE = "world_cup_2026"


def install(author_reports) -> None:
    """Freeze the accounting UI to the closed World Cup 2026 dataset."""

    def _wc_videos() -> list[dict[str, Any]]:
        from bot import handlers as h

        return db.fetch_all(
            h.VIDEO_SELECT
            + " WHERE v.status <> 'deleted' AND v.project_code = %s ORDER BY v.publish_date, v.created_at, v.id",
            (WORLD_CUP_CODE,),
        )

    def _period_options(videos, person, group_token=None):
        selected = videos
        if person is not None:
            selected = author_reports._person_activity_items(videos, person)
        elif group_token:
            group_people = author_reports._group_people(videos, group_token)
            selected = [
                video
                for video in videos
                if any(author_reports._roles_for_person(video, p) for p in group_people)
            ]
        months = sorted(
            {
                month
                for video in selected
                if (month := reconciliation.publish_month(video))
            },
            reverse=True,
        )
        options = [("🏆 ALL — финальный ЧМ 2026", "all")]
        options.extend(
            (author_reports._month_label(month).capitalize(), month)
            for month in months[:12]
        )
        return options

    def _period_filter(video, period: str) -> bool:
        if str(video.get("project_code") or "") != WORLD_CUP_CODE:
            return False
        if period in {"all", "wc"}:
            return True
        return reconciliation.publish_month(video) == period

    def _period_label(period: str, items):
        if period in {"all", "wc"}:
            label = "ЧМ 2026 — финальный итог"
        else:
            return author_reports._month_label(period)
        dates = [
            video.get("publish_date")
            for video in items
            if getattr(video.get("publish_date"), "strftime", None)
        ]
        if dates:
            return f"{label} ({min(dates).strftime('%d.%m.%Y')}–{max(dates).strftime('%d.%m.%Y')})"
        return label

    author_reports._active_videos = _wc_videos
    author_reports._period_options = _period_options
    author_reports._period_filter = _period_filter
    author_reports._period_label = _period_label
