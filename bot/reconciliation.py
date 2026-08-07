from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from typing import Any, Iterable

from psycopg.types.json import Jsonb

from bot import db, jobs
from bot.projects import PROJECTS, PROJECT_SHEET_TITLES, REPORTING_PROJECTS


ACTIVE_STATUS_SQL = "status <> 'deleted'"
PUBLISHED_STATUS = "approved"
WORKFLOW_STATUSES = {"pending", "needs_revision"}
BASE_MONTHS = ("2026-05", "2026-06", "2026-07", "2026-08")
NO_DATE_SHEET = "Без даты"
MONTH_RE = re.compile(r"^\d{4}-(?:0[1-9]|1[0-2])$")

MONTH_STATS_SHEET_NAME = "Month Stats"
UNFINISHED_SHEET_NAME = "Unfinished Requests"
UNSUBMITTED_SHEET_NAME = "Unsubmitted Forms"
RECONCILIATION_SHEET_NAME = "Reconciliation"
BACKFILL_REVIEW_SHEET_NAME = "Project Backfill Review"
AUTHOR_WORK_SHEET_NAME = "Работа авторов"
MONTAGE_WORK_SHEET_NAME = "Монтаж — справочно"

AUTHOR_WORK_COLUMNS = [
    "period",
    "author",
    "username",
    "project_code",
    "project_name",
    "regular_reels",
    "big_recaps",
    "total_authored",
]
MONTAGE_WORK_COLUMNS = [
    "period",
    "montage",
    "username",
    "project_code",
    "project_name",
    "regular_reels",
    "big_recaps",
    "total_montage",
]

DERIVED_VIDEO_COLUMNS = ["publish_month", "is_published", "is_incomplete", "missing_fields"]
PROJECT_STATS_COLUMNS = [
    "period",
    "project_code",
    "project_name",
    "active_records",
    "published_reels",
    "pending",
    "needs_revision",
    "duplicates",
    "missing_date",
    "incomplete_records",
    "authors_count",
    "montage_count",
    "voice_count",
    "updated_at",
]
MONTH_STATS_COLUMNS = [
    "period",
    "active_records",
    "published_reels",
    "pending",
    "needs_revision",
    "duplicates",
    "unassigned",
    "missing_date",
    "incomplete_records",
    "updated_at",
]
PEOPLE_PROJECTS_COLUMNS = [
    "period",
    "person_name",
    "person_username",
    "project_code",
    "project_name",
    "author_count",
    "montage_count",
    "voice_count",
    "approved_total",
]
UNFINISHED_COLUMNS = [
    "video_id",
    "status",
    "added_by",
    "added_by_tg_id",
    "project",
    "publish_date",
    "instagram_url",
    "youtube_url",
    "missing_fields",
    "return_reason",
    "comment",
    "created_at",
    "updated_at",
    "age_hours",
    "action_required",
]
UNSUBMITTED_COLUMNS = [
    "tg_id",
    "username",
    "state",
    "started_at",
    "updated_at",
    "age_hours",
    "instagram_id",
    "youtube_id",
    "project",
    "publish_date",
    "action_required",
]
RECONCILIATION_COLUMNS = ["metric", "database", "sheet", "difference", "status", "details"]
RECONCILIATION_PROBLEM_COLUMNS = [
    "problem_type",
    "video_id",
    "db_value",
    "sheet_value",
    "sheet_name",
    "details",
]
BACKFILL_REVIEW_COLUMNS = [
    "video_id",
    "status",
    "publish_date",
    "instagram_url",
    "youtube_url",
    "db_project_code",
    "db_project_name",
    "sheet_projects",
    "proposed_project_code",
    "proposed_project_name",
    "classification",
    "reason",
]

PROJECT_CODE_BY_SHEET = {title: code for code, title in PROJECT_SHEET_TITLES.items()}
PROJECT_NAMES = {
    str(project["code"]): (
        "Другие проекты" if project["code"] == "other" else str(project["name"])
    )
    for project in REPORTING_PROJECTS
}
PROJECT_NAMES["unassigned"] = "Без проекта"

CANONICAL_VIDEO_SELECT = """
SELECT
    v.*,
    COALESCE(v.author_name, author_p.name) AS author_name,
    COALESCE(v.author_username, author_p.username) AS author_username,
    author_p.tg_id AS author_tg_id,
    COALESCE(v.montage_name, montage_p.name) AS montage_name,
    COALESCE(v.montage_username, montage_p.username) AS montage_username,
    montage_p.tg_id AS montage_tg_id,
    COALESCE(v.voice_name, voice_p.name) AS voice_name,
    COALESCE(v.voice_username, voice_p.username) AS voice_username,
    voice_p.tg_id AS voice_tg_id
FROM videos v
LEFT JOIN people author_p ON author_p.id = v.author_id
LEFT JOIN people montage_p ON montage_p.id = v.montage_id
LEFT JOIN people voice_p ON voice_p.id = v.voice_id
"""


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def active_videos(videos: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [video for video in videos if str(video.get("status") or "") != "deleted"]


def project_partition_code(video: dict[str, Any]) -> str:
    code = str(video.get("project_code") or "").strip()
    if not code:
        return "unassigned"
    if code in PROJECT_SHEET_TITLES:
        return code
    return "other"


def project_partition_sheet(video: dict[str, Any]) -> str:
    return PROJECT_SHEET_TITLES[project_partition_code(video)]


def publish_month(video: dict[str, Any]) -> str | None:
    value = video.get("publish_date")
    if isinstance(value, datetime):
        return value.date().strftime("%Y-%m")
    if isinstance(value, date):
        return value.strftime("%Y-%m")
    text = str(value or "").strip()
    if len(text) >= 7 and MONTH_RE.match(text[:7]):
        return text[:7]
    return None


def month_partition_sheet(video: dict[str, Any]) -> str:
    return publish_month(video) or NO_DATE_SHEET


def missing_fields(video: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    if not str(video.get("project_code") or "").strip() or not str(
        video.get("project_name") or ""
    ).strip():
        missing.append("project")
    if not publish_month(video):
        missing.append("publish_date")
    video_type = "bigrecap" if video.get("video_type") == "bigrecap" else "regular"
    if video_type == "bigrecap":
        if not video.get("youtube_id") and not video.get("youtube_url"):
            missing.append("youtube")
    elif not video.get("instagram_id") and not video.get("instagram_url"):
        missing.append("instagram")
    if not video.get("author_id") and not str(video.get("author_name") or "").strip():
        missing.append("author")
    if not video.get("montage_id") and not str(video.get("montage_name") or "").strip():
        missing.append("montage")
    return missing


def derived_video_values(video: dict[str, Any]) -> dict[str, str]:
    missing = missing_fields(video)
    return {
        "publish_month": publish_month(video) or "",
        "is_published": _cell(video.get("status") == PUBLISHED_STATUS),
        "is_incomplete": _cell(bool(missing)),
        "missing_fields": ", ".join(missing),
    }


def canonical_sort_key(video: dict[str, Any]) -> tuple[str, str, int]:
    month_value = _cell(video.get("publish_date")) or "9999-99-99"
    created = _cell(video.get("created_at")) or "9999-99-99T99:99:99"
    return month_value, created, int(video.get("id") or 0)


def canonical_periods(videos: Iterable[dict[str, Any]]) -> list[str]:
    months = set(BASE_MONTHS)
    months.update(month for video in videos if (month := publish_month(video)))
    return ["ALL", *sorted(months), "NO_DATE"]


def _period_matches(video: dict[str, Any], period: str) -> bool:
    if period == "ALL":
        return True
    if period == "NO_DATE":
        return publish_month(video) is None
    return publish_month(video) == period


EGOR_CANONICAL_NAME = "Егор Петрушков"
EGOR_CANONICAL_USERNAME = "RayBallPro"
EGOR_NAME_ALIASES = {"егор", "егор петрушков"}


def _person_key(video: dict[str, Any], role: str) -> tuple[str, str] | None:
    name = str(video.get(f"{role}_name") or "").strip()
    if not name:
        return None
    username = str(video.get(f"{role}_username") or "").strip().lstrip("@")
    if username.casefold() == EGOR_CANONICAL_USERNAME.casefold() or name.casefold() in EGOR_NAME_ALIASES:
        return EGOR_CANONICAL_NAME, EGOR_CANONICAL_USERNAME
    return name, username


def _video_type_key(video: dict[str, Any]) -> str:
    return "bigrecap" if str(video.get("video_type") or "").lower() == "bigrecap" else "regular"


def build_project_stats_rows(
    videos: list[dict[str, Any]], *, updated_at: datetime | None = None
) -> list[list[str]]:
    current = updated_at or datetime.now(timezone.utc)
    active = active_videos(videos)
    rows: list[list[str]] = []
    project_codes = [str(item["code"]) for item in REPORTING_PROJECTS] + ["unassigned"]
    for period in canonical_periods(active):
        period_rows = [video for video in active if _period_matches(video, period)]
        for code in project_codes:
            selected = [video for video in period_rows if project_partition_code(video) == code]
            approved = [video for video in selected if video.get("status") == "approved"]
            people = {
                role: {person for video in approved if (person := _person_key(video, role))}
                for role in ("author", "montage", "voice")
            }
            rows.append(
                [
                    period,
                    code,
                    PROJECT_NAMES[code],
                    str(len(selected)),
                    str(len(approved)),
                    str(sum(video.get("status") == "pending" for video in selected)),
                    str(sum(video.get("status") == "needs_revision" for video in selected)),
                    str(sum(video.get("status") == "duplicate" for video in selected)),
                    str(sum(publish_month(video) is None for video in selected)),
                    str(sum(bool(missing_fields(video)) for video in selected)),
                    str(len(people["author"])),
                    str(len(people["montage"])),
                    str(len(people["voice"])),
                    current.isoformat(),
                ]
            )
    return rows


def build_month_stats_rows(
    videos: list[dict[str, Any]], *, updated_at: datetime | None = None
) -> list[list[str]]:
    current = updated_at or datetime.now(timezone.utc)
    active = active_videos(videos)
    rows: list[list[str]] = []
    for period in canonical_periods(active):
        selected = [video for video in active if _period_matches(video, period)]
        rows.append(
            [
                period,
                str(len(selected)),
                str(sum(video.get("status") == "approved" for video in selected)),
                str(sum(video.get("status") == "pending" for video in selected)),
                str(sum(video.get("status") == "needs_revision" for video in selected)),
                str(sum(video.get("status") == "duplicate" for video in selected)),
                str(sum(project_partition_code(video) == "unassigned" for video in selected)),
                str(sum(publish_month(video) is None for video in selected)),
                str(sum(bool(missing_fields(video)) for video in selected)),
                current.isoformat(),
            ]
        )
    return rows


def build_people_projects_rows(videos: list[dict[str, Any]]) -> list[list[str]]:
    active = active_videos(videos)
    counts: dict[tuple[str, str, str, str, str], dict[str, Any]] = defaultdict(
        lambda: {"author": 0, "montage": 0, "voice": 0, "video_ids": set()}
    )
    periods = canonical_periods(active)
    for video in active:
        if video.get("status") != "approved":
            continue
        code = project_partition_code(video)
        name = PROJECT_NAMES[code]
        video_periods = ["ALL", publish_month(video) or "NO_DATE"]
        for period in video_periods:
            if period not in periods:
                continue
            for role in ("author", "montage", "voice"):
                person = _person_key(video, role)
                if not person:
                    continue
                person_name, username = person
                item = counts[(period, person_name, username, code, name)]
                item[role] += 1
                item["video_ids"].add(int(video["id"]))
    rows: list[list[str]] = []
    for (period, name, username, code, project_name), item in sorted(counts.items()):
        rows.append(
            [
                period,
                name,
                f"@{username}" if username else "",
                code,
                project_name,
                str(item["author"]),
                str(item["montage"]),
                str(item["voice"]),
                str(len(item["video_ids"])),
            ]
        )
    return rows



def _build_role_work_rows(videos: list[dict[str, Any]], role: str) -> list[list[str]]:
    active = active_videos(videos)
    periods = canonical_periods(active)
    period_order = {period: index for index, period in enumerate(periods)}
    counts: dict[tuple[str, str, str, str, str], dict[str, int]] = defaultdict(
        lambda: {"regular": 0, "bigrecap": 0}
    )
    for video in active:
        if video.get("status") != PUBLISHED_STATUS:
            continue
        person = _person_key(video, role)
        if not person:
            continue
        person_name, username = person
        code = project_partition_code(video)
        project_name = PROJECT_NAMES[code]
        kind = _video_type_key(video)
        for period in ("ALL", publish_month(video) or "NO_DATE"):
            if period not in period_order:
                continue
            counts[(period, person_name, username, code, project_name)][kind] += 1

    ordered = sorted(
        counts.items(),
        key=lambda item: (
            period_order.get(item[0][0], 999),
            -sum(item[1].values()),
            item[0][1].casefold(),
            item[0][4].casefold(),
        ),
    )
    rows: list[list[str]] = []
    for (period, person_name, username, code, project_name), item in ordered:
        regular = int(item["regular"])
        bigrecap = int(item["bigrecap"])
        rows.append(
            [
                period,
                person_name,
                f"@{username}" if username else "",
                code,
                project_name,
                str(regular),
                str(bigrecap),
                str(regular + bigrecap),
            ]
        )
    return rows


def build_author_work_rows(videos: list[dict[str, Any]]) -> list[list[str]]:
    return _build_role_work_rows(videos, "author")


def build_montage_work_rows(videos: list[dict[str, Any]]) -> list[list[str]]:
    return _build_role_work_rows(videos, "montage")


def build_unfinished_rows(
    videos: list[dict[str, Any]], *, now: datetime | None = None
) -> list[list[str]]:
    current = now or datetime.now(timezone.utc)
    rows: list[list[str]] = []
    for video in sorted(active_videos(videos), key=canonical_sort_key):
        missing = missing_fields(video)
        status = str(video.get("status") or "")
        include = (
            status == "needs_revision"
            or (status == "pending" and bool(missing))
            or project_partition_code(video) == "unassigned"
            or publish_month(video) is None
        )
        if not include:
            continue
        created_at = video.get("created_at")
        if isinstance(created_at, datetime):
            aware = created_at if created_at.tzinfo else created_at.replace(tzinfo=timezone.utc)
            age_hours = max(0, int((current - aware.astimezone(timezone.utc)).total_seconds() / 3600))
        else:
            age_hours = 0
        reasons: list[str] = []
        if status == "needs_revision":
            reasons.append("returned_for_revision")
        if project_partition_code(video) == "unassigned":
            reasons.append("assign_project")
        if publish_month(video) is None:
            reasons.append("set_publish_date")
        if missing:
            reasons.append("complete_required_fields")
        added_by = f"@{video['added_by_username']}" if video.get("added_by_username") else ""
        rows.append(
            [
                _cell(video.get("id")),
                status,
                added_by,
                _cell(video.get("added_by_tg_id")),
                _cell(video.get("project_name")) or "Без проекта",
                _cell(video.get("publish_date")),
                _cell(video.get("instagram_url")),
                _cell(video.get("youtube_url")),
                ", ".join(missing),
                "returned_for_revision" if status == "needs_revision" else "",
                _cell(video.get("comment")),
                _cell(video.get("created_at")),
                _cell(video.get("updated_at")),
                str(age_hours),
                ", ".join(dict.fromkeys(reasons)),
            ]
        )
    return rows


def build_unsubmitted_rows(
    sessions: list[dict[str, Any]], *, now: datetime | None = None
) -> list[list[str]]:
    current = now or datetime.now(timezone.utc)
    rows: list[list[str]] = []
    for session in sessions:
        updated = session.get("updated_at")
        if isinstance(updated, datetime):
            aware = updated if updated.tzinfo else updated.replace(tzinfo=timezone.utc)
            age_hours = max(0, int((current - aware.astimezone(timezone.utc)).total_seconds() / 3600))
        else:
            age_hours = 0
        data = session.get("data") if isinstance(session.get("data"), dict) else {}
        rows.append(
            [
                _cell(session.get("tg_id")),
                f"@{session['username']}" if session.get("username") else "",
                _cell(session.get("state")),
                _cell(session.get("created_at")),
                _cell(updated),
                str(age_hours),
                _cell(data.get("instagram_id")),
                _cell(data.get("youtube_id")),
                _cell(data.get("project_name")),
                _cell(data.get("publish_date")),
                "finish_or_cancel_form",
            ]
        )
    return rows


SHEET_HEADER_SENTINELS = {"id", "period", "video_id", "tg_id", "metric", "captured_at"}


def _sheet_header_index(table: list[list[Any]] | None) -> int:
    if not table:
        return 0
    for index, row in enumerate(table[:8]):
        if row and str(row[0]).strip() in SHEET_HEADER_SENTINELS:
            return index
    return 0


def _sheet_rows(table: list[list[Any]] | None) -> tuple[list[str], list[list[str]]]:
    if not table:
        return [], []
    header_index = _sheet_header_index(table)
    header = [str(value).strip() for value in table[header_index]]
    rows = [[_cell(value) for value in row] for row in table[header_index + 1 :]]
    return header, rows


def _sheet_ids(table: list[list[Any]] | None) -> tuple[list[int], list[dict[str, Any]]]:
    header_index = _sheet_header_index(table)
    header, rows = _sheet_rows(table)
    id_index = header.index("id") if "id" in header else 0
    ids: list[int] = []
    problems: list[dict[str, Any]] = []
    for offset, row in enumerate(rows, start=header_index + 2):
        raw = row[id_index].strip() if id_index < len(row) else ""
        if not raw:
            if any(cell.strip() for cell in row):
                problems.append({"problem_type": "blank_id", "sheet_name": "", "details": f"row {offset}"})
            continue
        try:
            ids.append(int(raw))
        except ValueError:
            problems.append(
                {"problem_type": "invalid_id", "sheet_name": "", "sheet_value": raw, "details": f"row {offset}"}
            )
    return ids, problems


def _problem(
    problem_type: str,
    *,
    video_id: int | None = None,
    db_value: Any = "",
    sheet_value: Any = "",
    sheet_name: str = "",
    details: str = "",
) -> dict[str, Any]:
    return {
        "problem_type": problem_type,
        "video_id": video_id,
        "db_value": _cell(db_value),
        "sheet_value": _cell(sheet_value),
        "sheet_name": sheet_name,
        "details": details,
    }


def audit_sheet_tables(
    videos: list[dict[str, Any]],
    tables: dict[str, list[list[Any]]],
    *,
    video_columns: list[str],
) -> dict[str, Any]:
    from bot.sheets import (
        AUTHOR_WORK_SHEET_NAME,
        MONTAGE_WORK_SHEET_NAME,
        PROJECT_STATS_SHEET_NAME,
        SHEET_NAME,
        video_to_row,
    )

    active = sorted(active_videos(videos), key=canonical_sort_key)
    by_id = {int(video["id"]): video for video in active}
    db_ids = set(by_id)
    problems: list[dict[str, Any]] = []

    master_ids, master_parse = _sheet_ids(tables.get(SHEET_NAME))
    for item in master_parse:
        problems.append(_problem(item["problem_type"], sheet_name=SHEET_NAME, sheet_value=item.get("sheet_value"), details=item["details"]))
    master_counts = Counter(master_ids)
    for video_id, count in master_counts.items():
        if count > 1:
            problems.append(_problem("duplicate_in_videos", video_id=video_id, sheet_value=count, sheet_name=SHEET_NAME))
    missing_master = db_ids - set(master_ids)
    extra_master = set(master_ids) - db_ids
    for video_id in sorted(missing_master):
        problems.append(_problem("missing_from_videos", video_id=video_id, db_value="active", sheet_name=SHEET_NAME))
    for video_id in sorted(extra_master):
        problems.append(_problem("extra_in_videos", video_id=video_id, sheet_value="sheet-only/deleted", sheet_name=SHEET_NAME))

    master_header, master_rows = _sheet_rows(tables.get(SHEET_NAME))
    required_missing = [column for column in video_columns if column not in master_header]
    videos_header_mismatches = int(bool(required_missing))
    if required_missing:
        problems.append(_problem("videos_header_mismatch", db_value=",".join(video_columns), sheet_value=",".join(master_header), sheet_name=SHEET_NAME, details="missing: " + ", ".join(required_missing)))
    videos_row_mismatches = 0
    if "id" in master_header:
        id_index = master_header.index("id")
        expected_by_id = {
            int(video["id"]): dict(zip(video_columns, video_to_row(video, video_columns)))
            for video in active
        }
        common = [column for column in master_header if column in video_columns and column != "id"]
        for row in master_rows:
            try:
                video_id = int(row[id_index])
            except (IndexError, ValueError):
                continue
            if video_id not in expected_by_id:
                continue
            actual = {column: row[index] if index < len(row) else "" for index, column in enumerate(master_header)}
            differing = [column for column in common if actual.get(column, "") != expected_by_id[video_id].get(column, "")]
            if differing:
                videos_row_mismatches += 1
                problems.append(_problem("videos_row_mismatch", video_id=video_id, sheet_name=SHEET_NAME, details="columns: " + ", ".join(differing)))

    project_membership: dict[int, list[str]] = defaultdict(list)
    project_sheet_counts: dict[str, int] = {}
    project_invalid = 0
    for title in PROJECT_SHEET_TITLES.values():
        ids, parse_problems = _sheet_ids(tables.get(title))
        project_sheet_counts[title] = len(ids)
        project_invalid += len(parse_problems)
        for item in parse_problems:
            problems.append(_problem(item["problem_type"], sheet_name=title, sheet_value=item.get("sheet_value"), details=item["details"]))
        for video_id in ids:
            project_membership[video_id].append(title)
    project_union = set(project_membership)
    missing_projects = db_ids - project_union
    duplicate_projects = {video_id for video_id, memberships in project_membership.items() if len(memberships) > 1}
    project_extras = project_union - db_ids
    project_mismatches: set[int] = set()
    for video_id in sorted(db_ids):
        expected = project_partition_sheet(by_id[video_id])
        actual = project_membership.get(video_id, [])
        if actual != [expected]:
            project_mismatches.add(video_id)
            problems.append(_problem("project_membership_mismatch", video_id=video_id, db_value=expected, sheet_value=", ".join(actual), sheet_name=expected))
    for video_id in sorted(project_extras):
        problems.append(_problem("project_sheet_only_id", video_id=video_id, sheet_value=", ".join(project_membership[video_id])))

    expected_months = set(BASE_MONTHS)
    expected_months.update(month for video in active if (month := publish_month(video)))
    month_titles = sorted({title for title in tables if MONTH_RE.match(title)} | expected_months)
    month_titles.append(NO_DATE_SHEET)
    month_membership: dict[int, list[str]] = defaultdict(list)
    month_sheet_counts: dict[str, int] = {}
    month_invalid = 0
    for title in month_titles:
        ids, parse_problems = _sheet_ids(tables.get(title))
        month_sheet_counts[title] = len(ids)
        month_invalid += len(parse_problems)
        for item in parse_problems:
            problems.append(_problem(item["problem_type"], sheet_name=title, sheet_value=item.get("sheet_value"), details=item["details"]))
        for video_id in ids:
            month_membership[video_id].append(title)
    month_union = set(month_membership)
    missing_months = db_ids - month_union
    duplicate_months = {video_id for video_id, memberships in month_membership.items() if len(memberships) > 1}
    month_extras = month_union - db_ids
    month_mismatches: set[int] = set()
    for video_id in sorted(db_ids):
        expected = month_partition_sheet(by_id[video_id])
        actual = month_membership.get(video_id, [])
        if actual != [expected]:
            month_mismatches.add(video_id)
            problems.append(_problem("month_membership_mismatch", video_id=video_id, db_value=expected, sheet_value=", ".join(actual), sheet_name=expected))
    for video_id in sorted(month_extras):
        problems.append(_problem("month_sheet_only_id", video_id=video_id, sheet_value=", ".join(month_membership[video_id])))

    stats_mismatches = 0
    expected_reports = {
        PROJECT_STATS_SHEET_NAME: (PROJECT_STATS_COLUMNS, build_project_stats_rows(active)),
        MONTH_STATS_SHEET_NAME: (MONTH_STATS_COLUMNS, build_month_stats_rows(active)),
        AUTHOR_WORK_SHEET_NAME: (AUTHOR_WORK_COLUMNS, build_author_work_rows(active)),
        MONTAGE_WORK_SHEET_NAME: (MONTAGE_WORK_COLUMNS, build_montage_work_rows(active)),
    }
    for title, (columns, expected_rows) in expected_reports.items():
        header, actual_rows = _sheet_rows(tables.get(title))
        compare_columns = [column for column in columns if column != "updated_at"]
        if header != columns:
            stats_mismatches += 1
            problems.append(_problem("statistics_header_mismatch", db_value=", ".join(columns), sheet_value=", ".join(header), sheet_name=title))
            continue
        indexes = [columns.index(column) for column in compare_columns]
        actual_core = [[row[index] if index < len(row) else "" for index in indexes] for row in actual_rows]
        expected_core = [[row[index] for index in indexes] for row in expected_rows]
        if actual_core != expected_core:
            stats_mismatches += 1
            problems.append(_problem("statistics_values_mismatch", db_value=len(expected_rows), sheet_value=len(actual_rows), sheet_name=title))

    core_mismatch_count = sum(
        [
            len(missing_master),
            len(extra_master),
            sum(max(0, count - 1) for count in master_counts.values()),
            len(missing_projects),
            len(duplicate_projects),
            len(project_extras),
            len(project_mismatches),
            len(missing_months),
            len(duplicate_months),
            len(month_extras),
            len(month_mismatches),
            len(master_parse),
            project_invalid,
            month_invalid,
            videos_header_mismatches,
            videos_row_mismatches,
            stats_mismatches,
        ]
    )
    db_project_counts = Counter(project_partition_code(video) for video in active)
    db_month_counts = {
        period: {
            "active": sum(_period_matches(video, period) for video in active),
            "published": sum(
                _period_matches(video, period) and video.get("status") == PUBLISHED_STATUS
                for video in active
            ),
        }
        for period in canonical_periods(active)
    }
    return {
        "db_active_count": len(active),
        "db_approved_count": sum(video.get("status") == "approved" for video in active),
        "db_pending_count": sum(video.get("status") == "pending" for video in active),
        "db_needs_revision_count": sum(video.get("status") == "needs_revision" for video in active),
        "db_duplicate_count": sum(video.get("status") == "duplicate" for video in active),
        "db_unassigned_count": sum(project_partition_code(video) == "unassigned" for video in active),
        "db_missing_date_count": sum(publish_month(video) is None for video in active),
        "sheet_videos_count": len(master_ids),
        "sheet_videos_unique_count": len(set(master_ids)),
        "sheet_project_union_count": len(project_union),
        "sheet_month_union_count": len(month_union),
        "missing_from_videos": len(missing_master),
        "extra_in_videos": len(extra_master),
        "duplicate_in_videos": sum(max(0, count - 1) for count in master_counts.values()),
        "missing_from_projects": len(missing_projects),
        "duplicate_in_projects": len(duplicate_projects),
        "project_mismatches": len(project_mismatches),
        "project_sheet_only_ids": len(project_extras),
        "missing_from_months": len(missing_months),
        "duplicate_in_months": len(duplicate_months),
        "month_mismatches": len(month_mismatches),
        "month_sheet_only_ids": len(month_extras),
        "statistics_mismatches": stats_mismatches,
        "videos_header_mismatches": videos_header_mismatches,
        "videos_row_mismatches": videos_row_mismatches,
        "mismatch_count": core_mismatch_count,
        "problems": problems,
        "project_membership": {str(video_id): titles for video_id, titles in project_membership.items()},
        "month_membership": {str(video_id): titles for video_id, titles in month_membership.items()},
        "month_titles": month_titles,
        "project_sheet_counts": project_sheet_counts,
        "month_sheet_counts": month_sheet_counts,
        "db_project_counts": dict(db_project_counts),
        "db_month_counts": db_month_counts,
    }


def classify_project_backfills(
    videos: list[dict[str, Any]], project_membership: dict[str, list[str]]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for video in sorted(active_videos(videos), key=canonical_sort_key):
        video_id = int(video["id"])
        memberships = list(project_membership.get(str(video_id), []))
        codes = [PROJECT_CODE_BY_SHEET[title] for title in memberships if title in PROJECT_CODE_BY_SHEET]
        unknown_memberships = [title for title in memberships if title not in PROJECT_CODE_BY_SHEET]
        db_code = str(video.get("project_code") or "").strip()
        proposed = ""
        classification = "already_consistent"
        reason = "database assignment matches the recognized sheet"
        if not db_code:
            assignable = [code for code in codes if code != "unassigned"]
            if unknown_memberships and not codes:
                classification = "unknown_sheet"
                reason = "membership exists only in an unrecognized project sheet"
            elif unknown_memberships:
                classification = "conflict"
                reason = "recognized and unrecognized project-sheet evidence both exist"
            elif len(codes) == 1 and len(assignable) == 1:
                proposed = assignable[0]
                classification = "safe"
                reason = "blank database project and one recognized project-sheet membership"
            elif len(set(assignable)) > 1 or len(codes) > 1:
                classification = "conflict"
                reason = "blank database project has multiple project-sheet memberships"
            else:
                reason = "no unique recognized project-sheet evidence"
        else:
            expected = project_partition_code(video)
            if codes != [expected]:
                classification = "conflict"
                reason = "database project conflicts with current sheet membership"
        rows.append(
            {
                "video_id": video_id,
                "status": _cell(video.get("status")),
                "publish_date": _cell(video.get("publish_date")),
                "instagram_url": _cell(video.get("instagram_url")),
                "youtube_url": _cell(video.get("youtube_url")),
                "db_project_code": db_code,
                "db_project_name": _cell(video.get("project_name")),
                "sheet_projects": ", ".join(memberships),
                "proposed_project_code": proposed,
                "proposed_project_name": PROJECT_NAMES.get(proposed, ""),
                "classification": classification,
                "reason": reason,
            }
        )
    return rows


def backfill_review_row(item: dict[str, Any]) -> list[str]:
    return [_cell(item.get(column)) for column in BACKFILL_REVIEW_COLUMNS]


def reconciliation_rows(result: dict[str, Any]) -> list[list[str]]:
    metrics = [
        ("Videos unique IDs", result["db_active_count"], result.get("sheet_videos_unique_count")),
        ("Project union unique IDs", result["db_active_count"], result.get("sheet_project_union_count")),
        ("Month union unique IDs", result["db_active_count"], result.get("sheet_month_union_count")),
        ("Project duplicates", 0, result.get("duplicate_in_projects")),
        ("Project mismatches", 0, result.get("project_mismatches")),
        ("Month duplicates", 0, result.get("duplicate_in_months")),
        ("Month mismatches", 0, result.get("month_mismatches")),
        ("Sheet-only unknown IDs", 0, int(result.get("extra_in_videos") or 0) + int(result.get("project_sheet_only_ids") or 0) + int(result.get("month_sheet_only_ids") or 0)),
    ]
    rows = [
        [name, str(database), str(sheet), str(int(sheet or 0) - int(database or 0)), "PASS" if int(database or 0) == int(sheet or 0) else "FAIL", ""]
        for name, database, sheet in metrics
    ]
    rows.append(["", "", "", "", "", ""])
    rows.append(RECONCILIATION_PROBLEM_COLUMNS)
    rows.extend(
        [
            _cell(item.get("problem_type")),
            _cell(item.get("video_id")),
            _cell(item.get("db_value")),
            _cell(item.get("sheet_value")),
            _cell(item.get("sheet_name")),
            _cell(item.get("details")),
        ]
        for item in result.get("problems") or []
    )
    return rows


def load_active_video_snapshot(conn=None) -> list[dict[str, Any]]:
    sql = CANONICAL_VIDEO_SELECT + " WHERE v.status <> 'deleted' ORDER BY v.publish_date NULLS LAST, v.created_at, v.id"
    if conn is None:
        return db.fetch_all(sql)
    with conn.cursor() as cur:
        cur.execute(sql)
        return list(cur.fetchall())


def load_stale_unsubmitted_sessions(conn=None) -> list[dict[str, Any]]:
    sql = """
    SELECT s.*
    FROM user_sessions s
    WHERE (s.state LIKE 'new:%' OR s.state LIKE 'znambo:%')
      AND s.updated_at < now() - interval '60 minutes'
      AND NOT EXISTS (
          SELECT 1
          FROM videos v
          WHERE (NULLIF(s.data->>'instagram_id', '') IS NOT NULL AND v.instagram_id = s.data->>'instagram_id')
             OR (NULLIF(s.data->>'youtube_id', '') IS NOT NULL AND v.youtube_id = s.data->>'youtube_id')
      )
    ORDER BY s.updated_at, s.tg_id
    """
    if conn is None:
        return db.fetch_all(sql)
    with conn.cursor() as cur:
        cur.execute(sql)
        return list(cur.fetchall())


def create_audit_run(*, actor_tg_id: int, actor_username: str | None, chat_id: int) -> int:
    should_kick = False
    with db.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtext('sheets_reconciliation'))")
            cur.execute(
                """
                SELECT id, status
                FROM sheet_reconciliation_runs
                WHERE status IN ('created','auditing','awaiting_confirmation','rebuilding','validating')
                ORDER BY id DESC
                LIMIT 1
                FOR UPDATE
                """
            )
            existing = cur.fetchone()
            if existing:
                run_id = int(existing["id"])
                should_kick = existing.get("status") != "awaiting_confirmation"
            else:
                cur.execute(
                    """
                    INSERT INTO sheet_reconciliation_runs (
                        status, mode, initiated_by_tg_id, initiated_by_username,
                        initiated_chat_id, stage, started_at
                    )
                    VALUES ('created', 'audit', %s, %s, %s, 'queued', now())
                    RETURNING id
                    """,
                    (actor_tg_id, actor_username, chat_id),
                )
                run_id = int(cur.fetchone()["id"])
                should_kick = True
                jobs.enqueue_job(
                    "sheets_audit",
                    {"run_id": run_id},
                    dedupe_key=f"sheets:audit:{run_id}",
                    priority=50,
                    conn=conn,
                )
                db.log_event(
                    conn,
                    entity_type="sheet_reconciliation",
                    entity_id=run_id,
                    action="sheets_audit_queued",
                    actor_tg_id=actor_tg_id,
                    actor_username=actor_username,
                )
    if should_kick:
        from bot.worker_kick import kick_worker_if_ready

        kick_worker_if_ready(reason="enqueue:sheets_audit")
    return run_id


def get_run(run_id: int | None = None) -> dict[str, Any] | None:
    if run_id is not None:
        return db.fetch_one("SELECT * FROM sheet_reconciliation_runs WHERE id = %s", (run_id,))
    return db.fetch_one("SELECT * FROM sheet_reconciliation_runs ORDER BY id DESC LIMIT 1")


def run_mismatch_count(run: dict[str, Any]) -> int:
    summary = run.get("summary") if isinstance(run.get("summary"), dict) else {}
    if "mismatch_count" in summary:
        return int(summary.get("mismatch_count") or 0)
    return sum(
        int(run.get(field) or 0)
        for field in (
            "missing_from_videos",
            "extra_in_videos",
            "missing_from_projects",
            "duplicate_in_projects",
            "project_mismatches",
            "missing_from_months",
            "duplicate_in_months",
            "month_mismatches",
        )
    )


def format_run_summary(run: dict[str, Any]) -> str:
    return "\n".join(
        [
            f"Сверка Google Sheets #{run['id']}",
            "",
            f"Статус: {run.get('status')}",
            f"DB active: {int(run.get('db_active_count') or 0)}",
            f"Опубликовано: {int(run.get('db_approved_count') or 0)}",
            f"В работе: {int(run.get('db_pending_count') or 0)}",
            f"На доработке: {int(run.get('db_needs_revision_count') or 0)}",
            f"Дубли: {int(run.get('db_duplicate_count') or 0)}",
            f"Videos: {int(run.get('sheet_videos_count') or 0)}",
            f"Project union: {int(run.get('sheet_project_union_count') or 0)}",
            f"Month union: {int(run.get('sheet_month_union_count') or 0)}",
            f"Без проекта: {int(run.get('db_unassigned_count') or 0)}",
            f"Без даты: {int(run.get('db_missing_date_count') or 0)}",
            f"Safe project candidates: {int(run.get('safe_project_backfill_candidates') or 0)}",
            f"Conflicts: {int(run.get('conflicting_project_assignments') or 0)}",
            f"Расхождения: {run_mismatch_count(run)}",
        ]
    )


def audit_run(run_id: int, *, service=None) -> dict[str, Any]:
    from bot import sheets

    db.execute(
        "UPDATE sheet_reconciliation_runs SET status='auditing', stage='reading_snapshot', last_error=NULL, updated_at=now() WHERE id=%s",
        (run_id,),
    )
    with db.connect() as conn:
        conn.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
        videos = load_active_video_snapshot(conn)
        sessions = load_stale_unsubmitted_sessions(conn)
        conn.rollback()
    tables = sheets.read_reconciliation_tables(videos, service=service)
    result = audit_sheet_tables(videos, tables, video_columns=sheets.SHEET_COLUMNS)
    backfills = classify_project_backfills(videos, result["project_membership"])
    safe_count = sum(item["classification"] == "safe" for item in backfills)
    conflict_count = sum(item["classification"] == "conflict" for item in backfills)
    summary = {
        key: value
        for key, value in result.items()
        if key not in {"problems", "project_membership", "month_membership"}
    }
    summary.update(
        {
            "unfinished_request_count": len(build_unfinished_rows(videos)),
            "stale_session_count": len(sessions),
            "safe_project_backfill_candidates": safe_count,
            "conflicting_project_assignments": conflict_count,
        }
    )
    with db.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM sheet_reconciliation_items WHERE run_id=%s", (run_id,))
            problem_rows = [
                (run_id, "problem", item.get("sheet_name"), item.get("video_id"), index, Jsonb(_json_safe(item)))
                for index, item in enumerate(result["problems"])
            ]
            backfill_rows = [
                (run_id, "backfill", BACKFILL_REVIEW_SHEET_NAME, item["video_id"], index, Jsonb(_json_safe(item)))
                for index, item in enumerate(backfills)
            ]
            if problem_rows or backfill_rows:
                cur.executemany(
                    """
                    INSERT INTO sheet_reconciliation_items (
                        run_id, item_type, sheet_name, video_id, row_index, payload
                    ) VALUES (%s,%s,%s,%s,%s,%s)
                    """,
                    [*problem_rows, *backfill_rows],
                )
            cur.execute(
                """
                UPDATE sheet_reconciliation_runs
                SET status='awaiting_confirmation', stage='audit_done',
                    db_active_count=%s, db_approved_count=%s, db_pending_count=%s,
                    db_needs_revision_count=%s, db_duplicate_count=%s,
                    db_unassigned_count=%s, db_missing_date_count=%s,
                    sheet_videos_count=%s, sheet_project_union_count=%s,
                    sheet_month_union_count=%s, missing_from_videos=%s,
                    extra_in_videos=%s, missing_from_projects=%s,
                    duplicate_in_projects=%s, project_mismatches=%s,
                    missing_from_months=%s, duplicate_in_months=%s,
                    month_mismatches=%s, safe_project_backfill_candidates=%s,
                    conflicting_project_assignments=%s, summary=%s,
                    last_error=NULL, updated_at=now()
                WHERE id=%s
                """,
                (
                    result["db_active_count"],
                    result["db_approved_count"],
                    result["db_pending_count"],
                    result["db_needs_revision_count"],
                    result["db_duplicate_count"],
                    result["db_unassigned_count"],
                    result["db_missing_date_count"],
                    result["sheet_videos_count"],
                    result["sheet_project_union_count"],
                    result["sheet_month_union_count"],
                    result["missing_from_videos"],
                    result["extra_in_videos"],
                    result["missing_from_projects"],
                    result["duplicate_in_projects"],
                    result["project_mismatches"],
                    result["missing_from_months"],
                    result["duplicate_in_months"],
                    result["month_mismatches"],
                    safe_count,
                    conflict_count,
                    Jsonb(_json_safe(summary)),
                    run_id,
                ),
            )
            db.log_event(
                conn,
                entity_type="sheet_reconciliation",
                entity_id=run_id,
                action="sheets_audit_done",
                after_data=_json_safe(summary),
            )
    return get_run(run_id) or {}


def confirm_run(run_id: int, *, mode: str, actor_tg_id: int, actor_username: str | None) -> bool:
    if mode not in {"safe_backfill", "db_only"}:
        raise ValueError("unsupported reconciliation mode")
    with db.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM sheet_reconciliation_runs WHERE id=%s FOR UPDATE", (run_id,))
            run = cur.fetchone()
            if not run or run.get("status") != "awaiting_confirmation":
                return False
            cur.execute(
                """
                UPDATE sheet_reconciliation_runs
                SET status='created', mode=%s, stage='confirmed',
                    confirmed_by_tg_id=%s, confirmed_by_username=%s,
                    confirmed_at=now(), updated_at=now()
                WHERE id=%s
                """,
                (mode, actor_tg_id, actor_username, run_id),
            )
            jobs.enqueue_job(
                "sheets_reconcile",
                {"run_id": run_id},
                dedupe_key=f"sheets:reconcile:{run_id}",
                priority=55,
                conn=conn,
            )
            db.log_event(
                conn,
                entity_type="sheet_reconciliation",
                entity_id=run_id,
                action="sheets_reconcile_confirmed",
                actor_tg_id=actor_tg_id,
                actor_username=actor_username,
                after_data={"mode": mode},
            )
    from bot.worker_kick import kick_worker_if_ready

    kick_worker_if_ready(reason="enqueue:sheets_reconcile")
    return True


def cancel_run(run_id: int, *, actor_tg_id: int, actor_username: str | None) -> bool:
    with db.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE sheet_reconciliation_runs
                SET status='cancelled', stage='cancelled', finished_at=now(), updated_at=now()
                WHERE id=%s AND status='awaiting_confirmation'
                RETURNING id
                """,
                (run_id,),
            )
            changed = bool(cur.fetchone())
        if changed:
            db.log_event(
                conn,
                entity_type="sheet_reconciliation",
                entity_id=run_id,
                action="sheets_reconcile_cancelled",
                actor_tg_id=actor_tg_id,
                actor_username=actor_username,
            )
    return changed


def _load_backfill_items(conn, run_id: int) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT video_id, payload
            FROM sheet_reconciliation_items
            WHERE run_id=%s AND item_type='backfill'
            ORDER BY row_index
            """,
            (run_id,),
        )
        return list(cur.fetchall())


def _apply_safe_backfills(conn, run: dict[str, Any]) -> list[int]:
    if run.get("mode") != "safe_backfill":
        return []
    changed: list[int] = []
    for item in _load_backfill_items(conn, int(run["id"])):
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        if payload.get("classification") != "safe":
            continue
        code = str(payload.get("proposed_project_code") or "")
        if not code or code not in PROJECT_NAMES or code == "unassigned":
            continue
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE videos v
                SET project_code=%s,
                    project_name=%s,
                    project_id=(SELECT id FROM projects WHERE code=%s LIMIT 1),
                    sheet_sync_status='queued', sheet_sync_error=NULL, updated_at=now()
                WHERE v.id=%s AND v.status <> 'deleted'
                  AND COALESCE(v.project_code, '')=''
                RETURNING v.id
                """,
                (code, PROJECT_NAMES[code], code, int(item["video_id"])),
            )
            row = cur.fetchone()
        if not row:
            continue
        video_id = int(row["id"])
        changed.append(video_id)
        db.log_event(
            conn,
            entity_type="video",
            entity_id=video_id,
            action="project_backfilled_from_sheet",
            actor_tg_id=run.get("confirmed_by_tg_id"),
            actor_username=run.get("confirmed_by_username"),
            before_data={"project_code": None, "reconciliation_run_id": int(run["id"])},
            after_data={"project_code": code, "project_name": PROJECT_NAMES[code], "reconciliation_run_id": int(run["id"])},
        )
    return changed


def _expected_pass_result(active_count: int) -> dict[str, Any]:
    return {
        "db_active_count": active_count,
        "sheet_videos_unique_count": active_count,
        "sheet_project_union_count": active_count,
        "sheet_month_union_count": active_count,
        "duplicate_in_projects": 0,
        "project_mismatches": 0,
        "duplicate_in_months": 0,
        "month_mismatches": 0,
        "extra_in_videos": 0,
        "project_sheet_only_ids": 0,
        "month_sheet_only_ids": 0,
        "problems": [],
    }


def prepare_rebuild(run_id: int) -> dict[str, Any]:
    from bot import sheets

    with db.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM sheet_reconciliation_runs WHERE id=%s FOR UPDATE", (run_id,))
            run = cur.fetchone()
        if not run or run.get("status") not in {"created", "rebuilding"}:
            return run or {}
        changed_ids = _apply_safe_backfills(conn, run)
        videos = load_active_video_snapshot(conn)
        sessions = load_stale_unsubmitted_sessions(conn)
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM sheet_reconciliation_items WHERE run_id=%s AND item_type IN ('snapshot_video','managed_row')",
                (run_id,),
            )
            snapshot_rows = [
                (run_id, "snapshot_video", None, int(video["id"]), index, Jsonb(_json_safe(video)))
                for index, video in enumerate(videos)
            ]
            if snapshot_rows:
                cur.executemany(
                    """
                    INSERT INTO sheet_reconciliation_items
                        (run_id,item_type,sheet_name,video_id,row_index,payload)
                    VALUES (%s,%s,%s,%s,%s,%s)
                    """,
                    snapshot_rows,
                )

        original_backfills = _load_backfill_items(conn, run_id)
        backfill_payloads = [item["payload"] for item in original_backfills]
        specs = sheets.build_managed_sheet_specs(
            videos,
            sessions,
            backfill_payloads,
            reconciliation_rows(_expected_pass_result(len(videos))),
        )
        managed_rows: list[tuple[Any, ...]] = []
        for sheet_index, spec in enumerate(specs):
            for row_index, row in enumerate(spec["rows"]):
                managed_rows.append(
                    (
                        run_id,
                        "managed_row",
                        spec["name"],
                        int(row[0]) if row and str(row[0]).isdigit() else None,
                        row_index,
                        Jsonb({"row": _json_safe(row)}),
                    )
                )
        with conn.cursor() as cur:
            if managed_rows:
                cur.executemany(
                    """
                    INSERT INTO sheet_reconciliation_items
                        (run_id,item_type,sheet_name,video_id,row_index,payload)
                    VALUES (%s,%s,%s,%s,%s,%s)
                    """,
                    managed_rows,
                )
            previous_summary = run.get("summary") if isinstance(run.get("summary"), dict) else {}
            summary = {
                **previous_summary,
                "sheet_names": [spec["name"] for spec in specs],
                "sheet_columns": {spec["name"]: spec["columns"] for spec in specs},
                "changed_project_ids": changed_ids,
                "unfinished_request_count": len(build_unfinished_rows(videos)),
                "stale_session_count": len(sessions),
            }
            cur.execute(
                """
                UPDATE sheet_reconciliation_runs
                SET status='rebuilding', stage='staging', sheet_index=0, row_offset=0,
                    db_active_count=%s, db_approved_count=%s, db_pending_count=%s,
                    db_needs_revision_count=%s, db_duplicate_count=%s,
                    db_unassigned_count=%s, db_missing_date_count=%s,
                    summary=%s, last_error=NULL, updated_at=now()
                WHERE id=%s
                """,
                (
                    len(videos),
                    sum(video.get("status") == "approved" for video in videos),
                    sum(video.get("status") == "pending" for video in videos),
                    sum(video.get("status") == "needs_revision" for video in videos),
                    sum(video.get("status") == "duplicate" for video in videos),
                    sum(project_partition_code(video) == "unassigned" for video in videos),
                    sum(publish_month(video) is None for video in videos),
                    Jsonb(_json_safe(summary)),
                    run_id,
                ),
            )
            jobs.enqueue_job(
                "sheets_rebuild_chunk",
                {"run_id": run_id, "sheet_index": 0},
                dedupe_key=f"sheets:rebuild:{run_id}:0",
                priority=56,
                conn=conn,
            )
            db.log_event(
                conn,
                entity_type="sheet_reconciliation",
                entity_id=run_id,
                action="sheets_rebuild_started",
                after_data={"sheet_count": len(specs), "changed_project_ids": changed_ids},
            )
    return get_run(run_id) or {}


def rebuild_managed_sheets_from_db(run_id: int) -> dict[str, Any]:
    return prepare_rebuild(run_id)


def _managed_rows(run_id: int, sheet_name: str) -> list[list[str]]:
    rows = db.fetch_all(
        """
        SELECT payload
        FROM sheet_reconciliation_items
        WHERE run_id=%s AND item_type='managed_row' AND sheet_name=%s
        ORDER BY row_index
        """,
        (run_id, sheet_name),
    )
    return [list(row["payload"].get("row") or []) for row in rows]


def staging_title(run_id: int, sheet_index: int) -> str:
    return f"__tmp__r{run_id}_{sheet_index:02d}"


def rebuild_sheet_chunk(run_id: int, sheet_index: int, *, service=None) -> dict[str, Any]:
    from bot import sheets

    run = get_run(run_id)
    if not run or run.get("status") != "rebuilding":
        return run or {}
    summary = run.get("summary") if isinstance(run.get("summary"), dict) else {}
    names = list(summary.get("sheet_names") or [])
    columns_by_name = summary.get("sheet_columns") if isinstance(summary.get("sheet_columns"), dict) else {}
    current_index = int(run.get("sheet_index") or 0)
    if sheet_index < current_index:
        return run
    if sheet_index >= len(names):
        jobs.enqueue_job(
            "sheets_validate",
            {"run_id": run_id},
            dedupe_key=f"sheets:validate:{run_id}",
            priority=57,
        )
        return run
    sheet_name = names[sheet_index]
    rows = _managed_rows(run_id, sheet_name)
    sheets.write_staging_sheet(
        staging_title(run_id, sheet_index),
        list(columns_by_name[sheet_name]),
        rows,
        display_name=sheet_name,
        service=service,
    )
    next_index = sheet_index + 1
    with db.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE sheet_reconciliation_runs
                SET sheet_index=GREATEST(sheet_index,%s), row_offset=0,
                    stage=%s, updated_at=now()
                WHERE id=%s AND status='rebuilding'
                """,
                (next_index, f"staged:{sheet_name}", run_id),
            )
        if next_index < len(names):
            jobs.enqueue_job(
                "sheets_rebuild_chunk",
                {"run_id": run_id, "sheet_index": next_index},
                dedupe_key=f"sheets:rebuild:{run_id}:{next_index}",
                priority=56,
                conn=conn,
            )
        else:
            jobs.enqueue_job(
                "sheets_validate",
                {"run_id": run_id},
                dedupe_key=f"sheets:validate:{run_id}",
                priority=57,
                conn=conn,
            )
    return get_run(run_id) or {}


def _snapshot_videos(run_id: int) -> list[dict[str, Any]]:
    rows = db.fetch_all(
        """
        SELECT payload
        FROM sheet_reconciliation_items
        WHERE run_id=%s AND item_type='snapshot_video'
        ORDER BY row_index
        """,
        (run_id,),
    )
    return [dict(row["payload"]) for row in rows]


def _canonical_sheet_snapshot(videos: list[dict[str, Any]]) -> list[list[str]]:
    from bot import sheets

    return [
        sheets.video_to_row(video, sheets.SHEET_COLUMNS)
        for video in sorted(active_videos(videos), key=canonical_sort_key)
    ]


def validate_and_promote(run_id: int, *, service=None) -> dict[str, Any]:
    from bot import sheets

    run = get_run(run_id)
    if not run or run.get("status") not in {"rebuilding", "validating"}:
        return run or {}
    summary = run.get("summary") if isinstance(run.get("summary"), dict) else {}
    names = list(summary.get("sheet_names") or [])
    if int(run.get("sheet_index") or 0) < len(names):
        raise RuntimeError("staging is incomplete")
    db.execute(
        "UPDATE sheet_reconciliation_runs SET status='validating', stage='validating_staging', updated_at=now() WHERE id=%s",
        (run_id,),
    )
    videos = _snapshot_videos(run_id)
    current_videos = load_active_video_snapshot()
    if _canonical_sheet_snapshot(current_videos) != _canonical_sheet_snapshot(videos):
        with db.transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE sheet_reconciliation_runs
                    SET status='created', stage='snapshot_stale', sheet_index=0,
                        row_offset=0, last_error=NULL, updated_at=now()
                    WHERE id=%s
                    """,
                    (run_id,),
                )
            jobs.enqueue_job(
                "sheets_reconcile",
                {"run_id": run_id},
                dedupe_key=f"sheets:reconcile:{run_id}:refresh",
                priority=55,
                conn=conn,
            )
            db.log_event(
                conn,
                entity_type="sheet_reconciliation",
                entity_id=run_id,
                action="sheets_snapshot_refreshed",
                after_data={"reason": "database changed during staging"},
            )
        return get_run(run_id) or {}
    staging_map = {name: staging_title(run_id, index) for index, name in enumerate(names)}
    staged_tables = sheets.read_named_tables(list(staging_map.values()), service=service)
    staging_complete = all(staged_tables.get(temp) for temp in staging_map.values())
    if staging_complete:
        canonical_tables = {name: staged_tables.get(temp, []) for name, temp in staging_map.items()}
        staged_result = audit_sheet_tables(videos, canonical_tables, video_columns=sheets.SHEET_COLUMNS)
        if staged_result["mismatch_count"]:
            raise RuntimeError(
                f"staging validation failed: mismatches={staged_result['mismatch_count']}"
            )
        sheets.promote_staging_sheets(staging_map, run_id=run_id, service=service)
    final_videos = load_active_video_snapshot()
    final_tables = sheets.read_named_tables(names, service=service)
    final_result = audit_sheet_tables(final_videos, final_tables, video_columns=sheets.SHEET_COLUMNS)
    mismatch_count = int(final_result["mismatch_count"])
    if mismatch_count:
        raise RuntimeError(
            f"final validation failed: mismatches={mismatch_count}"
        )
    sheets.replace_reconciliation_result(reconciliation_rows(final_result), service=service)
    final_summary = {
        **summary,
        **{
            key: value
            for key, value in final_result.items()
            if key not in {"problems", "project_membership", "month_membership"}
        },
        "rebuilt_sheet_names": names,
        "mismatch_count": mismatch_count,
    }
    with db.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE sheet_reconciliation_runs
                SET status='done', stage='done', finished_at=now(),
                    sheet_videos_count=%s, sheet_project_union_count=%s,
                    sheet_month_union_count=%s, missing_from_videos=0,
                    extra_in_videos=0, missing_from_projects=0,
                    duplicate_in_projects=0, project_mismatches=0,
                    missing_from_months=0, duplicate_in_months=0,
                    month_mismatches=0, summary=%s, last_error=NULL, updated_at=now()
                WHERE id=%s
                """,
                (
                    final_result["sheet_videos_unique_count"],
                    final_result["sheet_project_union_count"],
                    final_result["sheet_month_union_count"],
                    Jsonb(_json_safe(final_summary)),
                    run_id,
                ),
            )
            db.log_event(
                conn,
                entity_type="sheet_reconciliation",
                entity_id=run_id,
                action="sheets_reconciliation_done",
                after_data={
                    "active_count": final_result["db_active_count"],
                    "mismatch_count": mismatch_count,
                    "changed_project_ids": summary.get("changed_project_ids") or [],
                },
            )
    return get_run(run_id) or {}


def mark_run_error(run_id: int, exc: Exception, *, terminal: bool = False) -> None:
    status = "failed" if terminal else None
    db.execute(
        """
        UPDATE sheet_reconciliation_runs
        SET status=COALESCE(%s,status), last_error=%s,
            finished_at=CASE WHEN %s IS NOT NULL THEN now() ELSE finished_at END,
            updated_at=now()
        WHERE id=%s
        """,
        (status, f"{type(exc).__name__}: {exc}"[:500], status, run_id),
    )
