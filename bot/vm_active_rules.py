from __future__ import annotations

from typing import Any

from bot import db
from bot import handlers as h
from bot import project_workflow_patch as workflow
from bot.telegram import TelegramClient, inline_keyboard


DANYA_NEMYKIN: dict[str, Any] = {
    "display_name": "Даня Немыкин",
    "display_username": "Ohluckylucky",
    "lookup_names": ("Даня Немыкин", "Даниил Немыкин", "Немыкин"),
    "lookup_username": "Ohluckylucky",
    "sort_weight": 60,
}

VM_ONLY_USERNAMES = {"sergeabaka", "ohluckylucky"}
_INSTALLED = False


def ensure_vm_roster() -> None:
    usernames = {
        str(item.get("lookup_username") or "").casefold()
        for item in workflow.VM_AUTHOR_ROSTER
    }
    if str(DANYA_NEMYKIN["lookup_username"]).casefold() not in usernames:
        workflow.VM_AUTHOR_ROSTER = (*workflow.VM_AUTHOR_ROSTER, DANYA_NEMYKIN)


def _send_help(tg: TelegramClient, actor: h.Actor) -> None:
    lines = [
        "Команды:",
        "/new_video — добавить Reels",
        "/new_aircut — отрез из эфира · Взял Мяч",
        "/cancel — отменить текущий сценарий",
        "/my_requests — мои заявки и дополнение ссылок",
        "Дата публикации обязательна при добавлении заявки.",
        "/chatid — показать ID текущего Telegram-чата",
        "/admin — очередь проверки",
        "/period_report [дата дата] — выгрузка за период",
        "/queue_status — статус очереди",
        "/queue_debug — диагностика FIFO-очереди",
        "/summary — сводка для админов",
        "/calendar — календарь публикаций",
        "/people — участники",
        "/person запрос — карточка участника",
        "/find запрос — точный поиск",
        "/daily_report [YYYY-MM-DD] — ежедневный отчёт",
        "/sync_sheets — повторная синхронизация Google Sheets",
        "/resend_pending — восстановить текущую FIFO-карточку",
        "/return_missing_dates — вернуть авторам заявки без даты",
        "/jobs_status — состояние фоновых заданий",
        "/sync_youtube_metrics — обновить YouTube-метрики",
        "/metrics_youtube_today — YouTube сегодня",
        "/metrics_youtube_all — YouTube всего",
        "/metrics_video id — метрики одного видео",
        "/worker_status — состояние worker",
    ]
    if h.is_superadmin(actor.tg_id):
        lines.extend(
            [
                "/kick_worker — разбудить event worker",
                "/run_jobs_now — инструкция ручного запуска worker",
                "",
                "Для суперадминов:",
                "/add_znambo — быстро добавить мой ролик",
                "/reset_admin_queue — сбросить и восстановить FIFO-очередь",
                "/queue_trace id — трассировка FIFO по заявке",
                "/retry_failed_jobs — повторить временно упавшие задания",
                "/add_person role name [tg_id] [@username]",
                "/activate_person id",
                "/deactivate_person id",
                "",
                "Роли: author, montage, voice, admin, superadmin.",
            ]
        )
    tg.send_message(actor.chat_id, "\n".join(lines))


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    ensure_vm_roster()
    original_ask_people = h.ask_people
    original_handle_message = h.handle_message
    original_handle_callback = h.handle_callback

    def send_main_menu(tg: TelegramClient, actor: h.Actor, text: str) -> None:
        rows = [
            [("➕ Добавить ролик", "cmd:new")],
            [("✂️ Отрез из эфира · Взял Мяч", "cmd:new_aircut")],
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
        tg.send_message(actor.chat_id, text, inline_keyboard(rows))

    def ask_people(
        tg: TelegramClient,
        actor: h.Actor,
        role: str,
        show_voice_decision: bool = True,
    ) -> None:
        if role != "author":
            original_ask_people(tg, actor, role, show_voice_decision)
            return

        project_code = workflow._project_code_from_session(actor)
        if project_code == workflow.VM_PROJECT_CODE:
            original_ask_people(tg, actor, role, show_voice_decision)
            return
        if project_code:
            people = [
                person
                for person in h.get_people("author")
                if str(person.get("username") or "").casefold()
                not in VM_ONLY_USERNAMES
            ]
            rows = workflow._author_rows([(None, person) for person in people])
            rows.append([("Нет в списке", "pm:a")])
            label = (
                "Выберите автора · ЧМ 2026."
                if project_code == workflow.WORLD_CUP_PROJECT_CODE
                else "Выберите автора."
            )
            tg.send_message(actor.chat_id, label, inline_keyboard(rows))
            return
        original_ask_people(tg, actor, role, show_voice_decision)

    def handle_message(message: dict[str, Any]) -> None:
        actor = h._actor_from_message(message)
        text = str(message.get("text") or "").strip()
        if actor and text.startswith("/"):
            command, _ = h._command_parts(text)
            if command == "/new_bigrecap":
                db.clear_session(actor.tg_id)
                TelegramClient().send_message(
                    actor.chat_id,
                    "Big Recap больше не используется. Добавьте ролик через /new_video.",
                )
                return
        original_handle_message(message)

    def handle_callback(callback: dict[str, Any]) -> None:
        actor = h._actor_from_callback(callback)
        data = str(callback.get("data") or "")
        if actor and data == "cmd:new_bigrecap":
            db.clear_session(actor.tg_id)
            tg = TelegramClient()
            try:
                tg.answer_callback_query(callback["id"])
            except Exception:
                pass
            tg.send_message(
                actor.chat_id,
                "Big Recap больше не используется. Добавьте ролик через /new_video.",
            )
            return
        original_handle_callback(callback)

    h._send_main_menu = send_main_menu
    h.send_help = _send_help
    h.ask_people = ask_people
    h.handle_message = handle_message
    h.handle_callback = handle_callback
    _INSTALLED = True
