from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    if old not in text:
        raise SystemExit(f"pattern not found in {path}: {old[:120]!r}")
    p.write_text(text.replace(old, new, 1), encoding="utf-8")


# Future YouTube syncs refresh one compact visible sheet instead of appending
# more rows to the raw Google Sheet. Historical daily snapshots stay in Postgres.
replace_once(
    "bot/metrics.py",
    "from bot import db, sheets, youtube_metrics",
    "from bot import db, metric_sheet, sheets, youtube_metrics",
)
replace_once(
    "bot/metrics.py",
    '''    try:\n        result.sheet_appended = sheets.append_metric_snapshots(ok_snapshots)\n        result.sheet_status = "ok"\n    except Exception as exc:\n        result.sheet_status = "failed"\n        result.sheet_error = _safe_error(exc)\n''',
    '''    try:\n        result.sheet_appended = metric_sheet.refresh_metric_summary()\n        result.sheet_status = "ok"\n    except Exception as exc:\n        result.sheet_status = "failed"\n        result.sheet_error = _safe_error(exc)\n''',
)
replace_once(
    "bot/metrics.py",
    '''    if result.sheet_status == "failed":\n        lines.extend(["", f"MetricsRaw: не обновлён ({result.sheet_error})"])\n    elif result.sheet_status == "ok":\n        lines.extend(["", f"MetricsRaw: добавлено строк {result.sheet_appended}"])\n''',
    '''    if result.sheet_status == "failed":\n        lines.extend(["", f"Метрики: не обновлены ({result.sheet_error})"])\n    elif result.sheet_status == "ok":\n        lines.extend(["", f"Метрики: роликов {result.sheet_appended}"])\n''',
)

# Plain-language description for the new human-facing page.
replace_once(
    "bot/sheet_layout.py",
    '''        "MetricsRaw": (\n            "Сырые метрики просмотров и реакций по роликам; это служебные данные для аналитики, "\n            "не итоговый отчёт команды."\n        ),\n''',
    '''        "Метрики": (\n            "Одна строка = один YouTube-ролик: текущие просмотры, лайки и комментарии плюс прирост "\n            "просмотров за 1, 7 и 30 дней; если истории за период ещё нет, стоит «—»."\n        ),\n        "MetricsRaw": (\n            "Скрытый старый технический архив ежедневных снимков метрик; для работы используйте вкладку «Метрики»."\n        ),\n''',
)

# Put visible metrics near the top and ignore hidden technical tabs when sorting.
replace_once(
    "bot/sheet_preambles.py",
    '''    "Работа авторов": 0,\n    "ЧМ 2026": 1,\n    "Монтаж — справочно": 2,\n    "Videos": 20,\n''',
    '''    "Работа авторов": 0,\n    "ЧМ 2026": 1,\n    "Метрики": 2,\n    "Монтаж — справочно": 3,\n    "Videos": 20,\n''',
)
replace_once(
    "bot/sheet_preambles.py",
    '''        if title and not title.startswith("__tmp__") and not title.startswith("__old__")\n''',
    '''        if title\n        and not bool(props.get("hidden"))\n        and not title.startswith("__tmp__")\n        and not title.startswith("__old__")\n''',
)

replace_once(
    "bot/version.py",
    'VERSION = "1.0.21"',
    'VERSION = "1.0.22"',
)

# Temporary OIDC allowance only for this one migration workflow. Cleanup restores it.
replace_once(
    "bot/github_oidc.py",
    'GITHUB_EVENTS = {"schedule", "workflow_dispatch"}',
    '# Temporary one-time allowance for metrics v1.0.22 migration.\nGITHUB_EVENTS = {"schedule", "workflow_dispatch", "push"}',
)

print("Prepared metrics v1.0.22")
