from __future__ import annotations

import hashlib
from collections import Counter
from datetime import date
from typing import Any

from bot import db, reconciliation
from bot.telegram import TelegramClient, inline_keyboard


MONTH_NAMES_RU = {
    1: "январь",
    2: "февраль",
    3: "март",
    4: "апрель",
    5: "май",
    6: "июнь",
    7: "июль",
    8: "август",
    9: "сентябрь",
    10: "октябрь",
    11: "ноябрь",
    12: "декабрь",
}

ROLES = ("author", "montage", "voice")
ROLE_COMBO_ORDER = (
    ("author", "montage", "voice"),
    ("author", "montage"),
    ("author", "voice"),
    ("montage", "voice"),
    ("author",),
    ("montage",),
    ("voice",),
)
ROLE_COMBO_LABELS = {
    ("author", "montage", "voice"): "Автор + монтаж + озвучка",
    ("author", "montage"): "Автор + монтаж",
    ("author", "voice"): "Автор + озвучка",
    ("montage", "voice"): "Монтаж + озвучка",
    ("author",): "Только автор",
    ("montage",): "Только монтаж",
    ("voice",): "Только озвучка",
}

_MENU_PATCHED = False


def _active_videos() -> list[dict[str, Any]]:
    from bot import handlers as h

    return db.fetch_all(
        h.VIDEO_SELECT + " WHERE v.status <> 'deleted' ORDER BY v.publish_date, v.created_at, v.id"
    )


def _role_key(video: dict[str, Any], role: str) -> tuple[str, str] | None:
    try:
        person = reconciliation._person_key(video, role)
    except Exception:
        person = None
    if person:
        return str(person[0]), str(person[1] or "").lstrip("@")
    name = str(video.get(f"{role}_name") or "").strip()
    if not name:
        return None
    username = str(video.get(f"{role}_username") or "").strip().lstrip("@")
    return name, username


def _person_token(person: tuple[str, str]) -> str:
    raw = f"{person[0].casefold()}|{person[1].casefold()}".encode("utf-8")
    return hashlib.blake2s(raw, digest_size=5).hexdigest()


def _person_map(videos: list[dict[str, Any]]) -> dict[str, tuple[str, str]]:
    people = sorted(
        {
            person
            for video in videos
            for role in ROLES
            if (person := _role_key(video, role))
        },
        key=lambda item: (item[0].casefold(), item[1].casefold()),
    )
    return {_person_token(person): person for person in people}


def _person_label(person: tuple[str, str]) -> str:
    name, username = person
    return f"{name} (@{username})" if username else name


def _roles_for_person(video: dict[str, Any], person: tuple[str, str]) -> tuple[str, ...]:
    return tuple(role for role in ROLES if _role_key(video, role) == person)


def _person_activity_items(
    videos: list[dict[str, Any]],
    person: tuple[str, str],
) -> list[dict[str, Any]]:
    return [video for video in videos if _roles_for_person(video, person)]


def _month_label(value: str) -> str:
    try:
        year, month = value.split("-", 1)
        return f"{MONTH_NAMES_RU[int(month)]} {year}"
    except Exception:
        return value


def _period_options(
    videos: list[dict[str, Any]],
    person: tuple[str, str] | None,
) -> list[tuple[str, str]]:
    selected = videos
    if person is not None:
        selected = _person_activity_items(videos, person)
    months = sorted(
        {month for video in selected if (month := reconciliation.publish_month(video))},
        reverse=True,
    )
    options: list[tuple[str, str]] = [
        ("🏆 ЧМ 2026 целиком", "wc"),
        ("За всё время", "all"),
    ]
    options.extend((_month_label(month).capitalize(), month) for month in months[:12])
    return options


def _period_filter(video: dict[str, Any], period: str) -> bool:
    if period == "all":
        return True
    if period == "wc":
        return str(video.get("project_code") or "") == "world_cup_2026"
    return reconciliation.publish_month(video) == period


def _period_label(period: str, items: list[dict[str, Any]]) -> str:
    if period == "wc":
        label = "ЧМ 2026 — весь проект"
    elif period == "all":
        label = "всё время"
    else:
        label = _month_label(period)

    dates = [
        video.get("publish_date")
        for video in items
        if isinstance(video.get("publish_date"), date)
    ]
    if dates and period in {"all", "wc"}:
        first = min(dates).strftime("%d.%m.%Y")
        last = max(dates).strftime("%d.%m.%Y")
        return f"{label} ({first}–{last})"
    return label


def _status_counts(items: list[dict[str, Any]]) -> dict[str, int]:
    statuses = [str(item.get("status") or "") for item in items]
    return {
        "total": len(items),
        "processed": sum(status != "pending" for status in statuses),
        "approved": sum(status == "approved" for status in statuses),
        "pending": sum(status == "pending" for status in statuses),
        "revision": sum(status == "needs_revision" for status in statuses),
        "duplicate": sum(status == "duplicate" for status in statuses),
    }


def _approved_combo_counts(
    items: list[dict[str, Any]],
    person: tuple[str, str],
) -> Counter[tuple[str, ...]]:
    counts: Counter[tuple[str, ...]] = Counter()
    for video in items:
        if video.get("status") != "approved":
            continue
        roles = _roles_for_person(video, person)
        if roles:
            counts[roles] += 1
    return counts


def build_forwardable_report(
    person: tuple[str, str],
    period: str,
    videos: list[dict[str, Any]],
) -> str:
    activity_items = _person_activity_items(videos, person)
    counts = _status_counts(activity_items)
    combo_counts = _approved_combo_counts(activity_items, person)

    lines = [
        "📊 СВЕРКА РАБОТ ДЛЯ БУХГАЛТЕРИИ",
        f"Сотрудник: {_person_label(person)}",
        f"Период: {_period_label(period, activity_items)}",
        "",
        f"Всего уникальных роликов с участием: {counts['total']}",
        f"Одобрено и учтено: {counts['approved']}",
        f"Ждёт проверки: {counts['pending']}",
        f"На доработке: {counts['revision']}",
        f"Дубликаты: {counts['duplicate']}",
    ]

    if counts["approved"]:
        lines.extend(["", "Что сделал в одобренных роликах:"])
        for combo in ROLE_COMBO_ORDER:
            value = combo_counts.get(combo, 0)
            if value:
                lines.append(f"• {ROLE_COMBO_LABELS[combo]}: {value}")

        combo_total = sum(combo_counts.values())
        if combo_total != counts["approved"]:
            lines.append(f"• Не удалось определить сочетание ролей: {counts['approved'] - combo_total}")

    lines.extend(
        [
            "",
            "Главная цифра для расчёта — одобренные уникальные ролики. Каждый ролик здесь считается один раз, даже если человек выполнил в нём несколько ролей.",
            "Период для месячных выборок считается по дате публикации ролика.",
        ]
    )
    return "\n".join(lines)


def start_author_report(tg: TelegramClient, actor) -> None:
    from bot import handlers as h

    if not h.require_admin(tg, actor):
        return
    videos = _active_videos()
    people = _person_map(videos)
    if not people:
        tg.send_message(actor.chat_id, "Участников работ в базе пока нет.")
        return
    rows: list[list[tuple[str, str]]] = [[("👥 Все сотрудники", "ar:a:all")]]
    for token, person in people.items():
        rows.append([(_person_label(person), f"ar:a:{token}")])
    tg.send_message(
        actor.chat_id,
        "👥 СВЕРКА РАБОТ\n\nВыберите сотрудника. Бот посчитает уникальные ролики без двойного счёта и покажет, какую комбинацию работ человек выполнил: автор, монтаж, озвучка или несколько ролей одновременно. Если выбрать «Все сотрудники», бот пришлёт отдельное пересылаемое сообщение по каждому.",
        inline_keyboard(rows),
    )


def _show_periods(tg: TelegramClient, actor, token: str) -> None:
    videos = _active_videos()
    people = _person_map(videos)
    person = None if token == "all" else people.get(token)
    if token != "all" and person is None:
        tg.send_message(actor.chat_id, "Список сотрудников изменился. Откройте сверку заново: /author_report")
        return
    rows = [
        [(label, f"ar:p:{token}:{period}")]
        for label, period in _period_options(videos, person)
    ]
    rows.append([("← К выбору сотрудника", "ar:start")])
    target = "всех сотрудников" if person is None else _person_label(person)
    tg.send_message(
        actor.chat_id,
        f"Проверяем: {target}.\n\nВыберите период. Месяцы считаются по дате публикации ролика:",
        inline_keyboard(rows),
    )


def _send_reports(tg: TelegramClient, actor, token: str, period: str) -> None:
    all_videos = _active_videos()
    videos = [video for video in all_videos if _period_filter(video, period)]
    people_map = _person_map(all_videos)
    if token == "all":
        people = sorted(
            {
                person
                for person in people_map.values()
                if _person_activity_items(videos, person)
            },
            key=lambda item: (item[0].casefold(), item[1].casefold()),
        )
    else:
        person = people_map.get(token)
        if person is None:
            tg.send_message(actor.chat_id, "Сотрудник больше не найден. Откройте сверку заново: /author_report")
            return
        people = [person]

    if not people:
        tg.send_message(actor.chat_id, "За выбранный период работ нет.")
        return

    if len(people) > 1:
        tg.send_message(
            actor.chat_id,
            f"Ниже будет {len(people)} отдельных сообщений — каждое можно переслать соответствующему сотруднику или в бухгалтерию.",
        )

    sent = 0
    for person in people:
        if not _person_activity_items(videos, person):
            continue
        tg.send_message(actor.chat_id, build_forwardable_report(person, period, videos))
        sent += 1

    tg.send_message(
        actor.chat_id,
        f"Готово: {sent} отчёт(ов).",
        inline_keyboard([[("Сверить ещё", "ar:start")]]),
    )


def handle_message(message: dict[str, Any]) -> bool:
    from bot import handlers as h

    actor = h._actor_from_message(message)
    if not actor:
        return False
    text = str(message.get("text") or "").strip()
    if not text.startswith("/"):
        return False
    command, _ = h._command_parts(text)
    if command not in {"/author_report", "/authors_check", "/author_check"}:
        return False
    start_author_report(TelegramClient(), actor)
    return True


def handle_callback(callback: dict[str, Any]) -> bool:
    from bot import handlers as h

    data = str(callback.get("data") or "")
    if not data.startswith("ar:"):
        return False
    actor = h._actor_from_callback(callback)
    if not actor:
        return True
    tg = TelegramClient()
    if not h.is_admin(actor.tg_id):
        try:
            tg.answer_callback_query(callback["id"], "Только для админов.", show_alert=True)
        except Exception:
            pass
        return True
    try:
        tg.answer_callback_query(callback["id"])
    except Exception:
        pass

    if data == "ar:start":
        start_author_report(tg, actor)
        return True
    if data.startswith("ar:a:"):
        _show_periods(tg, actor, data.split(":", 2)[2])
        return True
    if data.startswith("ar:p:"):
        parts = data.split(":", 3)
        if len(parts) != 4:
            tg.send_message(actor.chat_id, "Кнопка устарела. Откройте /author_report заново.")
            return True
        _send_reports(tg, actor, parts[2], parts[3])
        return True
    return True


def initialize_world_cup_unpaid_baseline() -> dict[str, Any]:
    from bot import admin_tools

    admin_tools.ensure_payment_schema()
    with db.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT count(*) AS count
                FROM videos
                WHERE status = 'approved'
                  AND project_code = 'world_cup_2026'
                """
            )
            total = int(cur.fetchone()["count"])
            cur.execute(
                """
                INSERT INTO video_payments (
                    video_id, is_paid, paid_at, paid_by_tg_id, paid_by_username,
                    note, created_at, updated_at
                )
                SELECT
                    id, false, NULL, NULL, NULL,
                    'Baseline 2026-08-18: World Cup payments have not started',
                    now(), now()
                FROM videos
                WHERE status = 'approved'
                  AND project_code = 'world_cup_2026'
                ON CONFLICT (video_id) DO UPDATE SET
                    is_paid = false,
                    paid_at = NULL,
                    paid_by_tg_id = NULL,
                    paid_by_username = NULL,
                    note = EXCLUDED.note,
                    updated_at = now()
                """
            )
            affected = int(cur.rowcount or 0)
        db.log_event(
            conn,
            entity_type="payment_baseline",
            entity_id=None,
            action="world_cup_all_marked_unpaid",
            after_data={"approved_world_cup": total, "affected": affected},
        )
    reporting = admin_tools.sync_reporting_sheets()
    return {"approved_world_cup": total, "affected": affected, "reporting": reporting}


def install_menu_patch() -> None:
    global _MENU_PATCHED
    if _MENU_PATCHED:
        return
    from bot import handlers as h, public_patch

    def _send_main_menu(tg: TelegramClient, actor, text: str) -> None:
        rows = [
            [("➕ Добавить ролик", "cmd:new")],
            [("🧵 Добавить большой рекап", "cmd:new_bigrecap")],
            [("📋 Мои заявки", "cmd:my"), ("ℹ️ Помощь", "cmd:help")],
        ]
        if h.is_superadmin(actor.tg_id):
            rows.append([("⚡ Добавить мой ролик", "cmd:add_znambo")])
        if h.is_admin(actor.tg_id):
            rows.insert(3, [("Админка", "cmd:admin"), ("Сводка", "cmd:summary")])
            rows.insert(4, [("👥 Сверка работ", "ar:start")])
            rows.insert(
                5,
                [
                    ("Статус очереди", "cmd:queue_status"),
                    ("Восстановить очередь", "cmd:resend_pending"),
                ],
            )
            rows.insert(6, [("Тест админ-чата", "cmd:test_admin_chat")])
        if h.is_superadmin(actor.tg_id):
            rows.append([("Сбросить FIFO-очередь", "cmd:reset_admin_queue")])
            status = "вкл" if public_patch._hearing_mode_enabled() else "выкл"
            rows.append([(f"👂 Режим «А?» сейчас: {status}", "fun:hearing:status")])
            rows.append(
                [
                    ("Включить режим «А?»", "fun:hearing:on"),
                    ("Выключить режим «А?»", "fun:hearing:off"),
                ]
            )
        tg.send_message(actor.chat_id, text, inline_keyboard(rows))

    h._send_main_menu = _send_main_menu
    _MENU_PATCHED = True
