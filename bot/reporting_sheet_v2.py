from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from typing import Any

from bot import multiplatform_metrics, sheets
from bot.config import get_settings
from bot.projects import PROJECTS


ARCHIVED_PROJECT_CODE = "world_cup_2026"
REPORT_SHEET = "🔎 Отчёт"
PROJECTS_SHEET = "📊 Проекты"
OPERATIONS_SHEETS = ("Заявки по авторам", "Выплаты")

PROJECT_TAB_BY_CODE = {
    "vzyal_myach": "🏀 Взял Мяч",
    "bolshe": "🎾 Больше",
    "ves_sport": "🌍 Весь Спорт",
    "padel_channel": "🎾 Padel Channel",
    "home_of_hockey": "🏒 Home of Hockey",
    "double_play": "🏈 Double Play",
    "sport_core": "👕 Sport Core",
    "music_core": "🎵 Music Core",
    "other": "📁 Другие",
}

PROJECT_ORDER = {
    str(project["code"]): int(project.get("sort_order") or 999)
    for project in PROJECTS
}
PROJECT_NAME_BY_CODE = {
    str(project["code"]): str(project["name"])
    for project in PROJECTS
}
PROJECT_NAME_BY_CODE["other"] = "Другие проекты"

PROJECT_COLUMNS = [
    "Дата",
    "ID",
    "Тип работы",
    "Автор",
    "Монтаж",
    "Озвучка",
    "Instagram",
    "IG просмотры",
    "YouTube",
    "YT просмотры",
    "TikTok",
    "TikTok просмотры",
    "VK",
    "VK просмотры",
    "Всего просмотров",
    "Покрытие",
    "Метрики обновлены",
]

REPORT_COLUMNS = [
    "Дата",
    "Проект",
    "Сотрудник",
    "Username",
    "Роль",
    "Тип работы",
    "ID",
    "Instagram",
    "IG просмотры",
    "YouTube",
    "YT просмотры",
    "TikTok",
    "TikTok просмотры",
    "VK",
    "VK просмотры",
    "Всего просмотров",
    "Покрытие",
]

SUMMARY_COLUMNS = [
    "Период",
    "Проект",
    "Ролики",
    "Отрезы из эфира",
    "Big Recap",
    "Всего работ",
    "Авторов",
    "IG просмотры",
    "YT просмотры",
    "TikTok просмотры",
    "VK просмотры",
    "Всего просмотров",
    "Покрытие",
]


def _text(value: Any) -> str:
    return str(value or "").strip()


def _as_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = _text(value)
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _project_code(video: dict[str, Any]) -> str:
    code = _text(video.get("project_code"))
    if code in PROJECT_TAB_BY_CODE:
        return code
    return "other"


def _project_name(video: dict[str, Any]) -> str:
    code = _project_code(video)
    if code == "other":
        return _text(video.get("project_name")) or PROJECT_NAME_BY_CODE["other"]
    return PROJECT_NAME_BY_CODE.get(code) or _text(video.get("project_name")) or code


def _is_aircut(video: dict[str, Any]) -> bool:
    video_type = _text(video.get("video_type")).casefold()
    if video_type == "aircut":
        return True
    return "отрез из эфира" in _text(video.get("comment")).casefold()


def _work_type(video: dict[str, Any]) -> str:
    if _text(video.get("video_type")).casefold() == "bigrecap":
        return "Big Recap"
    if _is_aircut(video):
        return "Отрез из эфира"
    return "Ролик"


def _person_parts(video: dict[str, Any], role: str) -> tuple[str, str]:
    name = _text(video.get(f"{role}_name"))
    username = _text(video.get(f"{role}_username")).lstrip("@")
    return name, username


def _person_label(video: dict[str, Any], role: str) -> str:
    name, username = _person_parts(video, role)
    if name and username:
        return f"{name} (@{username})"
    if name:
        return name
    if username:
        return f"@{username}"
    return ""


def active_reporting_videos(videos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected = [
        video
        for video in videos
        if _text(video.get("status")) == "approved"
        and _text(video.get("project_code")) != ARCHIVED_PROJECT_CODE
    ]

    def sort_key(video: dict[str, Any]) -> tuple[int, date, int]:
        published = _as_date(video.get("publish_date"))
        return (1 if published else 0, published or date.min, int(video.get("id") or 0))

    return sorted(selected, key=sort_key, reverse=True)


def _metric_values(
    video: dict[str, Any],
    latest: dict[tuple[int, str], dict[str, Any]],
) -> dict[str, Any]:
    return multiplatform_metrics._video_metric_values(video, latest)


def _views_cell(metric: dict[str, Any], platform: str) -> Any:
    value = metric.get(f"{platform}_views")
    return "" if value is None else int(value)


def build_project_rows(
    videos: list[dict[str, Any]],
    latest: dict[tuple[int, str], dict[str, Any]],
    project_code: str,
) -> list[list[Any]]:
    rows: list[list[Any]] = []
    for video in active_reporting_videos(videos):
        if _project_code(video) != project_code:
            continue
        metric = _metric_values(video, latest)
        rows.append(
            [
                _text(video.get("publish_date")),
                int(video.get("id") or 0),
                _work_type(video),
                _person_label(video, "author"),
                _person_label(video, "montage"),
                _person_label(video, "voice"),
                _text(video.get("instagram_url")),
                _views_cell(metric, "instagram"),
                _text(video.get("youtube_url")),
                _views_cell(metric, "youtube"),
                _text(video.get("tiktok_url")),
                _views_cell(metric, "tiktok"),
                _text(video.get("vk_url")),
                _views_cell(metric, "vk"),
                int(metric.get("total_known_views") or 0),
                multiplatform_metrics._coverage(video, latest),
                _text(metric.get("metrics_updated_at")),
            ]
        )
    return rows


def build_report_rows(
    videos: list[dict[str, Any]],
    latest: dict[tuple[int, str], dict[str, Any]],
) -> list[list[Any]]:
    rows: list[list[Any]] = []
    role_labels = (("author", "Автор"), ("montage", "Монтаж"), ("voice", "Озвучка"))
    for video in active_reporting_videos(videos):
        metric = _metric_values(video, latest)
        common = [
            _text(video.get("publish_date")),
            _project_name(video),
        ]
        tail = [
            _work_type(video),
            int(video.get("id") or 0),
            _text(video.get("instagram_url")),
            _views_cell(metric, "instagram"),
            _text(video.get("youtube_url")),
            _views_cell(metric, "youtube"),
            _text(video.get("tiktok_url")),
            _views_cell(metric, "tiktok"),
            _text(video.get("vk_url")),
            _views_cell(metric, "vk"),
            int(metric.get("total_known_views") or 0),
            multiplatform_metrics._coverage(video, latest),
        ]
        for role, role_label in role_labels:
            name, username = _person_parts(video, role)
            if not name and not username:
                continue
            employee = name or f"@{username}"
            rows.append([*common, employee, f"@{username}" if username else "", role_label, *tail])
    return rows


def build_summary_rows(
    videos: list[dict[str, Any]],
    latest: dict[tuple[int, str], dict[str, Any]],
) -> list[list[Any]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for video in active_reporting_videos(videos):
        published = _as_date(video.get("publish_date"))
        period = published.strftime("%Y-%m") if published else "Без даты"
        groups[(period, _project_code(video))].append(video)

    rows: list[list[Any]] = []
    ordered = sorted(
        groups.items(),
        key=lambda item: (
            item[0][0] != "Без даты",
            item[0][0] if item[0][0] != "Без даты" else "",
            -PROJECT_ORDER.get(item[0][1], 999),
        ),
        reverse=True,
    )
    for (period, code), items in ordered:
        types = [_work_type(video) for video in items]
        authors = {
            _person_parts(video, "author")
            for video in items
            if any(_person_parts(video, "author"))
        }
        platform_views: dict[str, int] = {platform: 0 for platform in multiplatform_metrics.PLATFORMS}
        supplied: dict[str, int] = {platform: 0 for platform in multiplatform_metrics.PLATFORMS}
        measured: dict[str, int] = {platform: 0 for platform in multiplatform_metrics.PLATFORMS}
        total_known = 0
        for video in items:
            video_id = int(video["id"])
            metric = _metric_values(video, latest)
            total_known += int(metric.get("total_known_views") or 0)
            for platform in multiplatform_metrics.PLATFORMS:
                if multiplatform_metrics._platform_id(video, platform):
                    supplied[platform] += 1
                snapshot = latest.get((video_id, platform))
                if snapshot is not None:
                    measured[platform] += 1
                    platform_views[platform] += int(snapshot.get("views") or 0)
        coverage = " · ".join(
            f"{label} {measured[platform]}/{supplied[platform]}"
            for platform, label in (("instagram", "IG"), ("youtube", "YT"), ("tiktok", "TT"), ("vk", "VK"))
        )
        project_name = (
            PROJECT_NAME_BY_CODE.get(code, "Другие проекты")
            if code != "other"
            else (_project_name(items[0]) if items else "Другие проекты")
        )
        rows.append(
            [
                period,
                project_name,
                types.count("Ролик"),
                types.count("Отрез из эфира"),
                types.count("Big Recap"),
                len(items),
                len(authors),
                platform_views["instagram"],
                platform_views["youtube"],
                platform_views["tiktok"],
                platform_views["vk"],
                total_known,
                coverage,
            ]
        )
    return rows


def _set_intro(service, spreadsheet_id: str, sheet_name: str, description: str) -> None:
    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=sheets._sheet_range(sheet_name, "A1:B1"),
        valueInputOption="RAW",
        body={"values": [["О вкладке", description]]},
    ).execute()


def _apply_filter_and_format(
    service,
    spreadsheet_id: str,
    sheet_name: str,
    column_count: int,
    row_count: int,
) -> None:
    properties = sheets._sheet_properties(service, spreadsheet_id).get(sheet_name)
    if not properties:
        return
    sheet_id = int(properties["sheetId"])
    requests: list[dict[str, Any]] = [
        {"clearBasicFilter": {"sheetId": sheet_id}},
        {
            "setBasicFilter": {
                "filter": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": 1,
                        "endRowIndex": max(2, row_count + 2),
                        "startColumnIndex": 0,
                        "endColumnIndex": column_count,
                    }
                }
            }
        },
        {
            "updateSheetProperties": {
                "properties": {
                    "sheetId": sheet_id,
                    "hidden": False,
                    "gridProperties": {"frozenRowCount": 2},
                },
                "fields": "hidden,gridProperties.frozenRowCount",
            }
        },
        {
            "autoResizeDimensions": {
                "dimensions": {
                    "sheetId": sheet_id,
                    "dimension": "COLUMNS",
                    "startIndex": 0,
                    "endIndex": column_count,
                }
            }
        },
    ]
    service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": requests},
    ).execute()


def _hide_legacy_and_reorder(
    service,
    spreadsheet_id: str,
    visible_order: list[str],
) -> None:
    properties = sheets._sheet_properties(service, spreadsheet_id)
    visible = set(visible_order)
    requests: list[dict[str, Any]] = []
    for title, item in properties.items():
        if title == "ЧМ 2026":
            requests.append({"deleteSheet": {"sheetId": int(item["sheetId"])}})
            continue
        should_hide = title not in visible
        if bool(item.get("hidden", False)) != should_hide:
            requests.append(
                {
                    "updateSheetProperties": {
                        "properties": {"sheetId": int(item["sheetId"]), "hidden": should_hide},
                        "fields": "hidden",
                    }
                }
            )
    for title in reversed([title for title in visible_order if title in properties]):
        requests.append(
            {
                "updateSheetProperties": {
                    "properties": {"sheetId": int(properties[title]["sheetId"]), "index": 0},
                    "fields": "index",
                }
            }
        )
    if requests:
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": requests},
        ).execute()


def sync(videos: list[dict[str, Any]], *, service=None) -> dict[str, int]:
    settings = get_settings()
    if not settings.google_sheets_spreadsheet_id:
        raise RuntimeError("GOOGLE_SHEETS_SPREADSHEET_ID is not configured")
    service = service or sheets._service()
    spreadsheet_id = settings.google_sheets_spreadsheet_id
    latest = multiplatform_metrics._latest_snapshots()

    sheet_specs: dict[str, tuple[list[str], list[list[Any]], str]] = {}
    for project in PROJECTS:
        code = str(project["code"])
        title = PROJECT_TAB_BY_CODE[code]
        rows = build_project_rows(videos, latest, code)
        sheet_specs[title] = (
            PROJECT_COLUMNS,
            rows,
            "Одобренные работы проекта. Самые свежие публикации всегда сверху; missing-метрика остаётся пустой, а не 0.",
        )

    report_rows = build_report_rows(videos, latest)
    sheet_specs[REPORT_SHEET] = (
        REPORT_COLUMNS,
        report_rows,
        "Одна строка = одна роль сотрудника в работе. Фильтруй по дате, проекту, сотруднику, роли и типу работы.",
    )
    summary_rows = build_summary_rows(videos, latest)
    sheet_specs[PROJECTS_SHEET] = (
        SUMMARY_COLUMNS,
        summary_rows,
        "Сводка активных проектов по месяцам: свежие периоды сверху, без lifetime ALL и без архивного ЧМ 2026.",
    )

    sheets._ensure_named_sheets(
        service,
        spreadsheet_id,
        {name: columns for name, (columns, _, _) in sheet_specs.items()},
    )
    for name, (columns, rows, description) in sheet_specs.items():
        sheets._replace_named_sheet(service, spreadsheet_id, name, columns, rows)
        _set_intro(service, spreadsheet_id, name, description)
        _apply_filter_and_format(service, spreadsheet_id, name, len(columns), len(rows))

    visible_order = [
        *(PROJECT_TAB_BY_CODE[str(project["code"])] for project in PROJECTS),
        REPORT_SHEET,
        PROJECTS_SHEET,
        *OPERATIONS_SHEETS,
    ]
    _hide_legacy_and_reorder(service, spreadsheet_id, visible_order)
    return {
        "active_projects": len(PROJECTS),
        "approved_active_videos": len(active_reporting_videos(videos)),
        "report_rows": len(report_rows),
        "summary_rows": len(summary_rows),
    }
