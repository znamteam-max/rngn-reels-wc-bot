from __future__ import annotations

from pathlib import Path


def replace_once(text: str, old: str, new: str, label: str) -> str:
    if old not in text:
        raise RuntimeError(f"pattern not found: {label}")
    return text.replace(old, new, 1)


def replace_block(text: str, start: str, end: str, new_block: str, label: str) -> str:
    a = text.find(start)
    if a < 0:
        raise RuntimeError(f"block start not found: {label}")
    b = text.find(end, a)
    if b < 0:
        raise RuntimeError(f"block end not found: {label}")
    return text[:a] + new_block + text[b:]


# ---------------------------------------------------------------------------
# reconciliation.py
# ---------------------------------------------------------------------------
p = Path("bot/reconciliation.py")
t = p.read_text(encoding="utf-8")

t = replace_once(
    t,
    'BACKFILL_REVIEW_SHEET_NAME = "Project Backfill Review"\n\nDERIVED_VIDEO_COLUMNS',
    'BACKFILL_REVIEW_SHEET_NAME = "Project Backfill Review"\n'
    'AUTHOR_WORK_SHEET_NAME = "Работа авторов"\n'
    'MONTAGE_WORK_SHEET_NAME = "Монтаж — справочно"\n\n'
    'AUTHOR_WORK_COLUMNS = [\n'
    '    "period",\n'
    '    "author",\n'
    '    "username",\n'
    '    "project_code",\n'
    '    "project_name",\n'
    '    "regular_reels",\n'
    '    "big_recaps",\n'
    '    "total_authored",\n'
    ']\n'
    'MONTAGE_WORK_COLUMNS = [\n'
    '    "period",\n'
    '    "montage",\n'
    '    "username",\n'
    '    "project_code",\n'
    '    "project_name",\n'
    '    "regular_reels",\n'
    '    "big_recaps",\n'
    '    "total_montage",\n'
    ']\n\n'
    'DERIVED_VIDEO_COLUMNS',
    "report sheet constants",
)

old_person = '''def _person_key(video: dict[str, Any], role: str) -> tuple[str, str] | None:\n    name = str(video.get(f"{role}_name") or "").strip()\n    if not name:\n        return None\n    username = str(video.get(f"{role}_username") or "").strip().lstrip("@")\n    return name, username\n\n\n'''
new_person = '''EGOR_CANONICAL_NAME = "Егор Петрушков"\nEGOR_CANONICAL_USERNAME = "RayBallPro"\nEGOR_NAME_ALIASES = {"егор", "егор петрушков"}\n\n\ndef _person_key(video: dict[str, Any], role: str) -> tuple[str, str] | None:\n    name = str(video.get(f"{role}_name") or "").strip()\n    if not name:\n        return None\n    username = str(video.get(f"{role}_username") or "").strip().lstrip("@")\n    if username.casefold() == EGOR_CANONICAL_USERNAME.casefold() or name.casefold() in EGOR_NAME_ALIASES:\n        return EGOR_CANONICAL_NAME, EGOR_CANONICAL_USERNAME\n    return name, username\n\n\ndef _video_type_key(video: dict[str, Any]) -> str:\n    return "bigrecap" if str(video.get("video_type") or "").lower() == "bigrecap" else "regular"\n\n\n'''
t = replace_once(t, old_person, new_person, "canonical person key")

insert_point = '\n\ndef build_unfinished_rows('
if insert_point not in t:
    raise RuntimeError("build_unfinished_rows insertion point not found")
work_builders = r'''


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
'''
t = t.replace(insert_point, work_builders + insert_point, 1)

start = 'def _sheet_rows(table: list[list[Any]] | None) -> tuple[list[str], list[list[str]]]:\n'
end = 'def _problem(\n'
new_sheet_rows = r'''SHEET_HEADER_SENTINELS = {"id", "period", "video_id", "tg_id", "metric", "captured_at"}


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


'''
t = replace_block(t, start, end, new_sheet_rows, "sheet rows parser")

t = replace_once(
    t,
    '    from bot.sheets import PEOPLE_PROJECTS_SHEET_NAME, PROJECT_STATS_SHEET_NAME, SHEET_NAME, video_to_row\n',
    '    from bot.sheets import (\n'
    '        AUTHOR_WORK_SHEET_NAME,\n'
    '        MONTAGE_WORK_SHEET_NAME,\n'
    '        PROJECT_STATS_SHEET_NAME,\n'
    '        SHEET_NAME,\n'
    '        video_to_row,\n'
    '    )\n',
    "audit imports",
)

t = replace_once(
    t,
    '''    expected_reports = {\n        PROJECT_STATS_SHEET_NAME: (PROJECT_STATS_COLUMNS, build_project_stats_rows(active)),\n        MONTH_STATS_SHEET_NAME: (MONTH_STATS_COLUMNS, build_month_stats_rows(active)),\n        PEOPLE_PROJECTS_SHEET_NAME: (PEOPLE_PROJECTS_COLUMNS, build_people_projects_rows(active)),\n    }\n''',
    '''    expected_reports = {\n        PROJECT_STATS_SHEET_NAME: (PROJECT_STATS_COLUMNS, build_project_stats_rows(active)),\n        MONTH_STATS_SHEET_NAME: (MONTH_STATS_COLUMNS, build_month_stats_rows(active)),\n        AUTHOR_WORK_SHEET_NAME: (AUTHOR_WORK_COLUMNS, build_author_work_rows(active)),\n        MONTAGE_WORK_SHEET_NAME: (MONTAGE_WORK_COLUMNS, build_montage_work_rows(active)),\n    }\n''',
    "expected reports",
)

t = replace_once(
    t,
    '''    sheets.write_staging_sheet(\n        staging_title(run_id, sheet_index),\n        list(columns_by_name[sheet_name]),\n        rows,\n        service=service,\n    )\n''',
    '''    sheets.write_staging_sheet(\n        staging_title(run_id, sheet_index),\n        list(columns_by_name[sheet_name]),\n        rows,\n        display_name=sheet_name,\n        service=service,\n    )\n''',
    "staging display name",
)
p.write_text(t, encoding="utf-8")


# ---------------------------------------------------------------------------
# sheets.py
# ---------------------------------------------------------------------------
p = Path("bot/sheets.py")
t = p.read_text(encoding="utf-8")

t = replace_once(t, "from bot import reconciliation\n", "from bot import reconciliation, sheet_layout\n", "sheet layout import")
t = replace_once(
    t,
    'PEOPLE_PROJECTS_SHEET_NAME = "People × Projects"\nMONTH_STATS_SHEET_NAME',
    'PEOPLE_PROJECTS_SHEET_NAME = "People × Projects"  # legacy sheet, removed after v1.0.20 rebuild\n'
    'AUTHOR_WORK_SHEET_NAME = reconciliation.AUTHOR_WORK_SHEET_NAME\n'
    'MONTAGE_WORK_SHEET_NAME = reconciliation.MONTAGE_WORK_SHEET_NAME\n'
    'MONTH_STATS_SHEET_NAME',
    "report sheet names",
)
t = replace_once(
    t,
    'PEOPLE_PROJECTS_COLUMNS = reconciliation.PEOPLE_PROJECTS_COLUMNS\nMETRICS_COLUMNS',
    'PEOPLE_PROJECTS_COLUMNS = reconciliation.PEOPLE_PROJECTS_COLUMNS\n'
    'AUTHOR_WORK_COLUMNS = reconciliation.AUTHOR_WORK_COLUMNS\n'
    'MONTAGE_WORK_COLUMNS = reconciliation.MONTAGE_WORK_COLUMNS\n'
    'METRICS_COLUMNS',
    "report sheet columns",
)

start = 'def _video_sheet_header(service, spreadsheet_id: str) -> list[str]:\n'
end = 'def _ensure_video_sheet_columns(service, spreadsheet_id: str) -> list[str]:\n'
new_video_header = r'''def _video_sheet_header(service, spreadsheet_id: str) -> list[str]:
    result = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=f"{SHEET_NAME}!A1:AZ4")
        .execute()
    )
    for row in result.get("values", []):
        values = [str(value).strip() for value in row]
        if "id" in values and "status" in values:
            return values
    return []


def _write_video_header(service, spreadsheet_id: str, columns: list[str]) -> None:
    end_column = _column_letter(len(columns))
    values = [*sheet_layout.preamble_rows(SHEET_NAME), columns]
    (
        service.spreadsheets()
        .values()
        .update(
            spreadsheetId=spreadsheet_id,
            range=f"{SHEET_NAME}!A1:{end_column}{sheet_layout.HEADER_ROW}",
            valueInputOption="USER_ENTERED",
            body={"values": values},
        )
        .execute()
    )
    _format_sheet_intro(service, spreadsheet_id, SHEET_NAME)


'''
t = replace_block(t, start, end, new_video_header, "video header block")

start = 'def _find_row_by_id(\n'
end = 'def _write_named_sheet_header(\n'
new_find = r'''def _find_row_by_id(
    service,
    spreadsheet_id: str,
    video_id: int,
    sheet_name: str = SHEET_NAME,
) -> int | None:
    result = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=_sheet_range(sheet_name, "A:A"))
        .execute()
    )
    for index, row in enumerate(result.get("values", []), start=1):
        if row and str(row[0]) == str(video_id):
            return index
    return None


'''
t = replace_block(t, start, end, new_find, "find row by id")

# Add formatting helper directly after _sheet_range.
needle = '''def _sheet_range(sheet_name: str, cells: str) -> str:\n    return f"'{sheet_name.replace(chr(39), chr(39) * 2)}'!{cells}"\n\n\n'''
format_helper = r'''def _sheet_range(sheet_name: str, cells: str) -> str:
    return f"'{sheet_name.replace(chr(39), chr(39) * 2)}'!{cells}"


def _format_sheet_intro(service, spreadsheet_id: str, sheet_name: str) -> None:
    properties = _sheet_properties(service, spreadsheet_id).get(sheet_name)
    if not properties:
        return
    sheet_id = int(properties["sheetId"])
    requests = [
        {
            "repeatCell": {
                "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 3},
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": {"red": 0.94, "green": 0.94, "blue": 0.94},
                        "wrapStrategy": "WRAP",
                        "verticalAlignment": "MIDDLE",
                    }
                },
                "fields": "userEnteredFormat(backgroundColor,wrapStrategy,verticalAlignment)",
            }
        },
        {
            "repeatCell": {
                "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1},
                "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                "fields": "userEnteredFormat.textFormat.bold",
            }
        },
        {
            "repeatCell": {
                "range": {"sheetId": sheet_id, "startRowIndex": 3, "endRowIndex": 4},
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": {"red": 0.86, "green": 0.89, "blue": 0.93},
                        "textFormat": {"bold": True},
                        "wrapStrategy": "WRAP",
                    }
                },
                "fields": "userEnteredFormat(backgroundColor,textFormat.bold,wrapStrategy)",
            }
        },
        {
            "updateSheetProperties": {
                "properties": {
                    "sheetId": sheet_id,
                    "gridProperties": {"frozenRowCount": sheet_layout.HEADER_ROW},
                },
                "fields": "gridProperties.frozenRowCount",
            }
        },
    ]
    service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": requests},
    ).execute()


'''
t = replace_once(t, needle, format_helper, "sheet range formatting helper")

start = 'def _write_named_sheet_header(\n'
end = 'def _ensure_named_sheets(\n'
new_named_header = r'''def _write_named_sheet_header(
    service,
    spreadsheet_id: str,
    sheet_name: str,
    columns: list[str],
) -> None:
    end_column = _column_letter(len(columns))
    values = [*sheet_layout.preamble_rows(sheet_name), columns]
    (
        service.spreadsheets()
        .values()
        .update(
            spreadsheetId=spreadsheet_id,
            range=_sheet_range(sheet_name, f"A1:{end_column}{sheet_layout.HEADER_ROW}"),
            valueInputOption="USER_ENTERED",
            body={"values": values},
        )
        .execute()
    )
    _format_sheet_intro(service, spreadsheet_id, sheet_name)


'''
t = replace_block(t, start, end, new_named_header, "named header block")

start = 'def _project_sheet_rows_by_id(\n'
end = 'def _managed_month_sheet_titles(\n'
new_project_rows = r'''def _project_sheet_rows_by_id(
    service,
    spreadsheet_id: str,
    project_sheets: list[str],
    video_id: int,
) -> dict[str, int]:
    if not project_sheets:
        return {}
    response = (
        service.spreadsheets()
        .values()
        .batchGet(
            spreadsheetId=spreadsheet_id,
            ranges=[_sheet_range(title, "A:A") for title in project_sheets],
            majorDimension="ROWS",
        )
        .execute()
    )
    value_ranges = response.get("valueRanges", [])
    found: dict[str, int] = {}
    for sheet_index, title in enumerate(project_sheets):
        rows = value_ranges[sheet_index].get("values", []) if sheet_index < len(value_ranges) else []
        for row_index, row in enumerate(rows, start=1):
            if row and str(row[0]) == str(video_id):
                found[title] = row_index
                break
    return found


'''
t = replace_block(t, start, end, new_project_rows, "partition rows lookup")

# Do not recreate every empty project tab on each incremental sync.
start = 'def _sync_video_project_sheet(\n'
end = 'def _sync_video_month_sheet(\n'
new_sync_project = r'''def _sync_video_project_sheet(
    service,
    spreadsheet_id: str,
    video: dict[str, Any],
    columns: list[str],
) -> None:
    all_project_sheets = list(PROJECT_SHEET_TITLES.values())
    target_title = project_sheet_title(str(video.get("project_code") or ""))
    properties = _sheet_properties(service, spreadsheet_id)
    project_sheets = [title for title in all_project_sheets if title in properties]
    if target_title and target_title not in properties:
        _ensure_named_sheets(service, spreadsheet_id, {target_title: columns})
        project_sheets.append(target_title)
    existing_rows = _project_sheet_rows_by_id(
        service,
        spreadsheet_id,
        project_sheets,
        int(video["id"]),
    )
    end_column = _column_letter(len(columns))
    clear_ranges = [
        _sheet_range(title, f"A{row_number}:{end_column}{row_number}")
        for title, row_number in existing_rows.items()
        if title != target_title
    ]
    if clear_ranges:
        service.spreadsheets().values().batchClear(
            spreadsheetId=spreadsheet_id,
            body={"ranges": clear_ranges},
        ).execute()
    if target_title:
        _write_video_to_named_sheet(
            service,
            spreadsheet_id,
            target_title,
            video,
            columns,
            row_number=existing_rows.get(target_title),
        )


'''
t = replace_block(t, start, end, new_sync_project, "incremental project sync")

start = 'def _remove_video_from_managed_sheets(\n'
end = 'def upsert_video(video: dict[str, Any], *, service=None) -> int:\n'
new_remove = r'''def _remove_video_from_managed_sheets(
    service,
    spreadsheet_id: str,
    video_id: int,
    columns: list[str],
) -> None:
    properties = _sheet_properties(service, spreadsheet_id)
    project_titles = [title for title in PROJECT_SHEET_TITLES.values() if title in properties]
    month_titles = [title for title in _managed_month_sheet_titles(service, spreadsheet_id) if title in properties]
    titles = [SHEET_NAME, *project_titles, *month_titles]
    existing_rows = _project_sheet_rows_by_id(
        service,
        spreadsheet_id,
        list(dict.fromkeys(title for title in titles if title in properties)),
        video_id,
    )
    if not existing_rows:
        return
    end_column = _column_letter(len(columns))
    service.spreadsheets().values().batchClear(
        spreadsheetId=spreadsheet_id,
        body={
            "ranges": [
                _sheet_range(title, f"A{row_number}:{end_column}{row_number}")
                for title, row_number in existing_rows.items()
            ]
        },
    ).execute()


'''
t = replace_block(t, start, end, new_remove, "remove managed sheets")

t = replace_once(
    t,
    '    row_number = video.get("sheet_row") or _find_row_by_id(service, spreadsheet_id, int(video["id"]))\n',
    '    row_number = _find_row_by_id(service, spreadsheet_id, int(video["id"]))\n',
    "ignore stale sheet_row",
)

# Batch upsert: only existing/target project tabs, scan full A column, respect row-5 data start.
t = replace_once(
    t,
    '    project_sheets = list(PROJECT_SHEET_TITLES.values())\n    month_sheets = _managed_month_sheet_titles(service, spreadsheet_id, videos)\n',
    '    properties = _sheet_properties(service, spreadsheet_id)\n'
    '    target_project_sheets = {\n'
    '        reconciliation.project_partition_sheet(video)\n'
    '        for video in videos\n'
    '        if video.get("status") != "deleted"\n'
    '    }\n'
    '    project_sheets = [\n'
    '        title for title in PROJECT_SHEET_TITLES.values()\n'
    '        if title in properties or title in target_project_sheets\n'
    '    ]\n'
    '    month_sheets = _managed_month_sheet_titles(service, spreadsheet_id, videos)\n',
    "batch project sheets",
)
t = t.replace('ranges=[_sheet_range(title, "A2:A") for title in sheet_names],', 'ranges=[_sheet_range(title, "A:A") for title in sheet_names],', 1)
t = replace_once(t, '            for row_number, row in enumerate(rows, start=2)\n', '            for row_number, row in enumerate(rows, start=1)\n', "batch row enumerate")
t = replace_once(
    t,
    '    next_row = {title: len(rows_by_sheet[title]) + 2 for title in sheet_names}\n',
    '    next_row = {\n'
    '        title: max(sheet_layout.DATA_START_ROW, len(rows_by_sheet[title]) + 1)\n'
    '        for title in sheet_names\n'
    '    }\n',
    "batch next row",
)
t = replace_once(
    t,
    '        main_row = int(video.get("sheet_row") or index_by_sheet[SHEET_NAME].get(video_id) or 0)\n',
    '        main_row = int(index_by_sheet[SHEET_NAME].get(video_id) or 0)\n',
    "batch ignore stale sheet_row",
)

start = 'def _replace_named_sheet(\n'
end = 'def sync_project_reports(videos: list[dict[str, Any]], *, service=None) -> None:\n'
new_replace = r'''def _replace_named_sheet(
    service,
    spreadsheet_id: str,
    sheet_name: str,
    columns: list[str],
    rows: list[list[str]],
    *,
    display_name: str | None = None,
) -> None:
    end_column = _column_letter(len(columns))
    intro_name = display_name or sheet_name
    values = [*sheet_layout.preamble_rows(intro_name), columns, *rows]
    (
        service.spreadsheets()
        .values()
        .clear(
            spreadsheetId=spreadsheet_id,
            range=_sheet_range(sheet_name, f"A:{end_column}"),
            body={},
        )
        .execute()
    )
    (
        service.spreadsheets()
        .values()
        .update(
            spreadsheetId=spreadsheet_id,
            range=_sheet_range(sheet_name, f"A1:{end_column}{len(rows) + sheet_layout.HEADER_ROW}"),
            valueInputOption="RAW",
            body={"values": values},
        )
        .execute()
    )
    _format_sheet_intro(service, spreadsheet_id, sheet_name)


'''
t = replace_block(t, start, end, new_replace, "replace named sheet")

start = 'def sync_project_reports(videos: list[dict[str, Any]], *, service=None) -> None:\n'
end = 'def read_named_tables(\n'
new_sync_reports = r'''def sync_project_reports(videos: list[dict[str, Any]], *, service=None) -> None:
    settings = get_settings()
    if not settings.google_sheets_spreadsheet_id:
        raise RuntimeError("GOOGLE_SHEETS_SPREADSHEET_ID is not configured")
    service = service or _service()
    spreadsheet_id = settings.google_sheets_spreadsheet_id
    _ensure_named_sheets(
        service,
        spreadsheet_id,
        {
            PROJECT_STATS_SHEET_NAME: PROJECT_STATS_COLUMNS,
            MONTH_STATS_SHEET_NAME: MONTH_STATS_COLUMNS,
            AUTHOR_WORK_SHEET_NAME: AUTHOR_WORK_COLUMNS,
            MONTAGE_WORK_SHEET_NAME: MONTAGE_WORK_COLUMNS,
        },
    )
    _replace_named_sheet(
        service,
        spreadsheet_id,
        PROJECT_STATS_SHEET_NAME,
        PROJECT_STATS_COLUMNS,
        build_project_stats_rows(videos),
    )
    _replace_named_sheet(
        service,
        spreadsheet_id,
        MONTH_STATS_SHEET_NAME,
        MONTH_STATS_COLUMNS,
        reconciliation.build_month_stats_rows(videos),
    )
    _replace_named_sheet(
        service,
        spreadsheet_id,
        AUTHOR_WORK_SHEET_NAME,
        AUTHOR_WORK_COLUMNS,
        reconciliation.build_author_work_rows(videos),
    )
    _replace_named_sheet(
        service,
        spreadsheet_id,
        MONTAGE_WORK_SHEET_NAME,
        MONTAGE_WORK_COLUMNS,
        reconciliation.build_montage_work_rows(videos),
    )


'''
t = replace_block(t, start, end, new_sync_reports, "sync project reports")

t = replace_once(
    t,
    '        PEOPLE_PROJECTS_SHEET_NAME,\n    ]\n',
    '        AUTHOR_WORK_SHEET_NAME,\n        MONTAGE_WORK_SHEET_NAME,\n    ]\n',
    "reconciliation report names",
)

old_project_specs = '''    for title in PROJECT_SHEET_TITLES.values():\n        rows = [\n            video_to_row(video, SHEET_COLUMNS)\n            for video in active\n            if reconciliation.project_partition_sheet(video) == title\n        ]\n        specs.append({"name": title, "columns": SHEET_COLUMNS, "rows": rows})\n'''
new_project_specs = '''    always_keep_project_sheets = {"ЧМ 2026", "Весь Спорт"}\n    for title in PROJECT_SHEET_TITLES.values():\n        rows = [\n            video_to_row(video, SHEET_COLUMNS)\n            for video in active\n            if reconciliation.project_partition_sheet(video) == title\n        ]\n        if rows or title in always_keep_project_sheets:\n            specs.append({"name": title, "columns": SHEET_COLUMNS, "rows": rows})\n'''
t = replace_once(t, old_project_specs, new_project_specs, "managed project specs")

t = replace_once(
    t,
    '''            {\n                "name": PEOPLE_PROJECTS_SHEET_NAME,\n                "columns": PEOPLE_PROJECTS_COLUMNS,\n                "rows": reconciliation.build_people_projects_rows(active),\n            },\n''',
    '''            {\n                "name": AUTHOR_WORK_SHEET_NAME,\n                "columns": AUTHOR_WORK_COLUMNS,\n                "rows": reconciliation.build_author_work_rows(active),\n            },\n            {\n                "name": MONTAGE_WORK_SHEET_NAME,\n                "columns": MONTAGE_WORK_COLUMNS,\n                "rows": reconciliation.build_montage_work_rows(active),\n            },\n''',
    "managed work reports",
)

start = 'def write_staging_sheet(\n'
end = 'def promote_staging_sheets(\n'
new_staging = r'''def write_staging_sheet(
    sheet_name: str,
    columns: list[str],
    rows: list[list[str]],
    *,
    display_name: str | None = None,
    service=None,
) -> None:
    settings = get_settings()
    if not settings.google_sheets_spreadsheet_id:
        raise RuntimeError("GOOGLE_SHEETS_SPREADSHEET_ID is not configured")
    service = service or _service()
    spreadsheet_id = settings.google_sheets_spreadsheet_id
    _ensure_named_sheets(service, spreadsheet_id, {sheet_name: columns})
    _replace_named_sheet(
        service,
        spreadsheet_id,
        sheet_name,
        columns,
        rows,
        display_name=display_name,
    )


'''
t = replace_block(t, start, end, new_staging, "write staging sheet")

# Metrics use the same readable 3-row intro/header layout.
start = 'def _ensure_metrics_sheet(service, spreadsheet_id: str) -> None:\n'
end = 'def _metric_date_key(value: Any) -> str:\n'
new_metrics_ensure = r'''def _ensure_metrics_sheet(service, spreadsheet_id: str) -> None:
    properties = _sheet_properties(service, spreadsheet_id)
    if METRICS_SHEET_NAME not in properties:
        _ensure_named_sheets(
            service,
            spreadsheet_id,
            {METRICS_SHEET_NAME: METRICS_COLUMNS},
        )
        return
    # Existing legacy MetricsRaw is normalized explicitly during reconciliation migration.


def normalize_metrics_sheet_layout(*, service=None) -> None:
    settings = get_settings()
    if not settings.google_sheets_spreadsheet_id:
        raise RuntimeError("GOOGLE_SHEETS_SPREADSHEET_ID is not configured")
    service = service or _service()
    spreadsheet_id = settings.google_sheets_spreadsheet_id
    _ensure_metrics_sheet(service, spreadsheet_id)
    table = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=_sheet_range(METRICS_SHEET_NAME, "A:O"))
        .execute()
        .get("values", [])
    )
    header_index = None
    for index, row in enumerate(table[:8]):
        if row and str(row[0]).strip() == "captured_at":
            header_index = index
            break
    rows = table[header_index + 1 :] if header_index is not None else []
    _replace_named_sheet(
        service,
        spreadsheet_id,
        METRICS_SHEET_NAME,
        METRICS_COLUMNS,
        [[_as_cell(value) for value in row] for row in rows],
    )


'''
t = replace_block(t, start, end, new_metrics_ensure, "metrics layout")

start = 'def _existing_metric_keys(service, spreadsheet_id: str) -> set[tuple[str, str, str]]:\n'
end = 'def metric_snapshot_to_row(snapshot: dict[str, Any]) -> list[str]:\n'
new_metric_keys = r'''def _existing_metric_keys(service, spreadsheet_id: str) -> set[tuple[str, str, str]]:
    result = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=f"{METRICS_SHEET_NAME}!A:O")
        .execute()
    )
    rows = result.get("values", [])
    header_index = next(
        (index for index, row in enumerate(rows[:8]) if row and str(row[0]).strip() == "captured_at"),
        -1,
    )
    keys: set[tuple[str, str, str]] = set()
    for row in rows[header_index + 1 :]:
        if len(row) >= 3:
            keys.add((str(row[1]), str(row[2]), _metric_date_key(row[0])))
    return keys


'''
t = replace_block(t, start, end, new_metric_keys, "metric key parser")

# Add controlled cleanup for obsolete empty project tabs and the legacy mixed people report.
needle = 'def _sheet_titles(service, spreadsheet_id: str) -> set[str]:\n'
cleanup = r'''def cleanup_empty_report_tabs(videos: list[dict[str, Any]], *, service=None) -> list[str]:
    settings = get_settings()
    if not settings.google_sheets_spreadsheet_id:
        raise RuntimeError("GOOGLE_SHEETS_SPREADSHEET_ID is not configured")
    service = service or _service()
    spreadsheet_id = settings.google_sheets_spreadsheet_id
    properties = _sheet_properties(service, spreadsheet_id)
    counts = defaultdict(int)
    for video in reconciliation.active_videos(videos):
        counts[reconciliation.project_partition_sheet(video)] += 1
    keep_even_if_empty = {"ЧМ 2026", "Весь Спорт"}
    delete_titles = [
        title
        for title in PROJECT_SHEET_TITLES.values()
        if title in properties and not counts.get(title) and title not in keep_even_if_empty
    ]
    if PEOPLE_PROJECTS_SHEET_NAME in properties:
        delete_titles.append(PEOPLE_PROJECTS_SHEET_NAME)
    delete_titles = list(dict.fromkeys(delete_titles))
    if delete_titles:
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={
                "requests": [
                    {"deleteSheet": {"sheetId": int(properties[title]["sheetId"])}}
                    for title in delete_titles
                ]
            },
        ).execute()
    return delete_titles


'''
t = t.replace(needle, cleanup + needle, 1)

p.write_text(t, encoding="utf-8")


# ---------------------------------------------------------------------------
# version.py
# ---------------------------------------------------------------------------
p = Path("bot/version.py")
p.write_text('VERSION = "1.0.20"\n', encoding="utf-8")

print("Prepared v1.0.20 report layout patch")
