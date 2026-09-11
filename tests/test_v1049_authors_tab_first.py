from bot.active_author_performance import _target_sheet_index


def test_authors_sheet_always_targets_first_tab():
    properties = {
        "🔎 Отчёт": {"index": 9},
        "👤 Авторы": {"index": 10},
        "🏀 Взял Мяч": {"index": 0},
    }
    assert _target_sheet_index(properties) == 0
