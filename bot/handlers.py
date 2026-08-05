from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

import psycopg

from bot import db, jobs, metrics
from bot.config import get_settings
from bot.daily_reports import preview_daily_report, previous_report_date
from bot.links import (
    extract_youtube_id,
    is_skip_text,
    normalize_instagram,
    normalize_optional,
    normalize_tiktok,
    normalize_vk,
    normalize_youtube,
    parse_publish_date,
)
from bot.messages import (
    format_batch_summary,
    format_final_card,
    format_video_card,
    person_display,
    person_value,
    user_label,
)
from bot.projects import PROJECTS, normalize_custom_project_name
from bot.telegram import TelegramAPIError, TelegramClient, inline_keyboard


ROLE_BY_SHORT = {"a": "author", "m": "montage", "v": "voice"}
SHORT_BY_ROLE = {value: key for key, value in ROLE_BY_SHORT.items()}
PERSON_USAGE_COLUMN = {
    "author": "author_id",
    "montage": "montage_id",
    "voice": "voice_id",
}
VIDEO_TYPE_REGULAR = "regular"
VIDEO_TYPE_BIGRECAP = "bigrecap"
VIDEO_TYPES = {VIDEO_TYPE_REGULAR, VIDEO_TYPE_BIGRECAP}
PLATFORM_FLOW_REGULAR = "instagram_first"
PLATFORM_FLOW_BIGRECAP = "youtube_vk"
BIGRECAP_YOUTUBE_PROMPT = "Пришли ссылку на YouTube-ролик большого рекапа"
BIGRECAP_YOUTUBE_INVALID_MESSAGE = (
    "Это не похоже на ссылку YouTube. Пришли ссылку вида "
    "youtube.com/watch?v=..., youtu.be/... или youtube.com/shorts/..."
)
ADD_ZNAMBO_SESSION_INSTAGRAM = "znambo:instagram"
ADD_ZNAMBO_SESSION_DATE = "znambo:date"
ADD_ZNAMBO_SESSION_PROJECT = "znambo:project"
ADD_ZNAMBO_SESSION_PROJECT_OTHER = "znambo:project_other"
ADD_ZNAMBO_UNAUTHORIZED_MESSAGE = "Команда доступна только суперадмину."
ADD_ZNAMBO_LINK_PROMPT = "Пришли ссылку на Instagram/Reels"
ADD_ZNAMBO_INVALID_LINK_MESSAGE = "Это не похоже на ссылку Instagram/Reels. Пришли корректную ссылку."
ADD_ZNAMBO_DATE_PROMPT = "Укажи дату публикации"
ADD_ZNAMBO_MANUAL_DATE_PROMPT = (
    "Введи дату публикации: YYYY-MM-DD, DD.MM или D.M.\n"
    "Например: 2026-07-16 или 16.07"
)
ADD_ZNAMBO_INVALID_DATE_MESSAGE = "Не понял дату. Используй ДД.ММ или ГГГГ-ММ-ДД."
ADD_ZNAMBO_NAME = "Знамбо"
ADD_ZNAMBO_USERNAME = "znambo"
ADD_ZNAMBO_SORT_WEIGHTS = {"author": 100, "montage": 100, "voice": 20}
ADD_ZNAMBO_DATE_PRESETS = {
    "today": "Сегодня",
    "yesterday": "Вчера",
}
NEW_DATE_SESSION = "new:date"
NEW_DATE_MANUAL_SESSION = "new:date_manual"
NEW_DATE_PROMPT = "Укажи дату публикации ролика"
NEW_DATE_MANUAL_PROMPT = (
    "Введи дату публикации: YYYY-MM-DD, DD.MM или D.M.\n"
    "Например: 2026-08-03 или 03.08"
)
NEW_DATE_INVALID_MESSAGE = "Не понял дату. Используй ДД.ММ или ГГГГ-ММ-ДД."
NEW_DATE_PRESETS = {"today": "Сегодня", "yesterday": "Вчера"}
PROJECT_PROMPT = "Для какого проекта сделан ролик?"
PROJECT_OTHER_PROMPT = "Напиши название проекта"
PROJECT_OTHER_INVALID_MESSAGE = "Название проекта должно содержать от 2 до 60 символов."
ADMIN_QUEUE_NAME = "main"
ADMIN_DATE_CLAIM_SECONDS = 300
ADMIN_RESET_ARCHIVE_LIMIT = 8
ADMIN_DATE_PROMPT = "Сегодня, Вчера, Позавчера, YYYY-MM-DD, DD.MM или D.M."
ADMIN_QUEUE_STALE_MESSAGE = "Эта карточка устарела. Открой актуальную очередь: /admin"
ADMIN_QUEUE_FILTER_TYPES = {"global", "project", "other", "unassigned"}

VIDEO_SELECT = """
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

PENDING_VIDEOS_SQL = (
    VIDEO_SELECT
    + """
WHERE v.status = 'pending'
ORDER BY v.created_at ASC, v.id ASC
"""
)


@dataclass(frozen=True)
class Actor:
    tg_id: int
    chat_id: int
    username: str | None = None
    first_name: str | None = None
    chat_type: str = "private"
    chat_title: str | None = None


def handle_update(update: dict[str, Any]) -> None:
    if "message" in update:
        handle_message(update["message"])
    elif "callback_query" in update:
        handle_callback(update["callback_query"])


def _actor_from_message(message: dict[str, Any]) -> Actor | None:
    user = message.get("from")
    chat = message.get("chat")
    if not user or not chat:
        return None
    return Actor(
        tg_id=int(user["id"]),
        chat_id=int(chat["id"]),
        username=user.get("username"),
        first_name=user.get("first_name"),
        chat_type=chat.get("type", "private"),
        chat_title=chat.get("title"),
    )


def _actor_from_callback(callback: dict[str, Any]) -> Actor | None:
    user = callback.get("from")
    message = callback.get("message") or {}
    chat = message.get("chat")
    if not user or not chat:
        return None
    return Actor(
        tg_id=int(user["id"]),
        chat_id=int(chat["id"]),
        username=user.get("username"),
        first_name=user.get("first_name"),
        chat_type=chat.get("type", "private"),
        chat_title=chat.get("title"),
    )


def _safe_error(exc: Exception) -> str:
    text = str(exc).replace(get_settings().bot_token or "", "[token]")
    return text[:500]


def telegram_failure_payload(
    exc: Exception,
    admin_chat_id: int,
    stage: str = "send",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "admin_chat_id": admin_chat_id,
        "stage": stage,
        "error": _safe_error(exc),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if isinstance(exc, TelegramAPIError):
        payload["telegram_status_code"] = exc.status_code
        payload["telegram_description"] = exc.description
        payload["telegram_retry_after"] = exc.retry_after
    return payload


def _message_id(response: dict[str, Any]) -> int | None:
    message = response.get("result") if isinstance(response, dict) else None
    if not isinstance(message, dict) or message.get("message_id") is None:
        return None
    return int(message["message_id"])


def store_admin_message(video_id: int, chat_id: int, response: dict[str, Any]) -> None:
    db.execute(
        """
        UPDATE videos
        SET admin_message_chat_id = %s,
            admin_message_id = %s,
            admin_notified_at = now(),
            updated_at = now()
        WHERE id = %s
        """,
        (chat_id, _message_id(response), video_id),
    )


def admin_delivery_message_matches(
    video: dict[str, Any],
    chat_id: int,
    message_id: int | None,
) -> bool:
    if not message_id:
        return False
    return (
        int(video.get("admin_message_chat_id") or 0) == int(chat_id)
        and int(video.get("admin_message_id") or 0) == int(message_id)
    )


def _command_parts(text: str) -> tuple[str, str]:
    first, _, rest = text.strip().partition(" ")
    command = first.split("@", 1)[0].lower()
    return command, rest.strip()


def build_chatid_text(chat: dict[str, Any], user: dict[str, Any]) -> str:
    username = user.get("username")
    lines = [
        f"chat_id: {chat.get('id')}",
        f"chat_type: {chat.get('type', 'unknown')}",
        f"title: {chat.get('title', '')}",
        f"from_id: {user.get('id')}",
        f"from_username: @{username}" if username else "from_username: ",
    ]
    return "\n".join(lines)


def normalize_video_type(value: Any) -> str:
    text = str(value or VIDEO_TYPE_REGULAR).strip().lower()
    return text if text in VIDEO_TYPES else VIDEO_TYPE_REGULAR


def send_chatid(
    tg: TelegramClient,
    actor: Actor,
    chat: dict[str, Any],
    user: dict[str, Any],
) -> None:
    text = build_chatid_text(chat, user)
    tg.send_message(actor.chat_id, text)
    record_system_log(
        "chatid_requested",
        "telegram_chat",
        None,
        {
            "chat_id": chat.get("id"),
            "chat_type": chat.get("type"),
            "title": chat.get("title"),
            "from_id": user.get("id"),
            "from_username": user.get("username"),
        },
        actor,
    )


def _send_main_menu(tg: TelegramClient, actor: Actor, text: str) -> None:
    rows = [
        [("➕ Добавить ролик", "cmd:new")],
        [("🧵 Добавить большой рекап", "cmd:new_bigrecap")],
        [("📋 Мои заявки", "cmd:my"), ("ℹ️ Помощь", "cmd:help")],
    ]
    if is_superadmin(actor.tg_id):
        rows.append([("⚡ Добавить мой ролик", "cmd:add_znambo")])
    if is_admin(actor.tg_id):
        rows.insert(3, [("Админка", "cmd:admin"), ("Сводка", "cmd:summary")])
        rows.insert(4, [("Статус очереди", "cmd:queue_status"), ("Восстановить очередь", "cmd:resend_pending")])
    if is_superadmin(actor.tg_id):
        rows.append([("Сбросить FIFO-очередь", "cmd:reset_admin_queue")])
    tg.send_message(actor.chat_id, text, inline_keyboard(rows))


def handle_message(message: dict[str, Any]) -> None:
    actor = _actor_from_message(message)
    if not actor:
        return
    text = (message.get("text") or "").strip()
    if not text:
        return
    tg = TelegramClient()

    if text.startswith("/"):
        command, rest = _command_parts(text)
        if command == "/start":
            db.clear_session(actor.tg_id)
            _send_main_menu(
                tg,
                actor,
                "Привет! Я собираю заявки на Reels и помогаю админам быстро переносить проверенные видео в отчёт.",
            )
        elif command == "/help":
            send_help(tg, actor)
        elif command == "/new_video":
            start_new_video(tg, actor)
        elif command == "/new_bigrecap":
            start_new_bigrecap(tg, actor)
        elif command == "/add_znambo":
            start_add_znambo(tg, actor)
        elif command == "/chatid":
            send_chatid(tg, actor, message.get("chat") or {}, message.get("from") or {})
        elif command == "/my_requests":
            show_my_requests(tg, actor)
        elif command == "/admin":
            show_admin(tg, actor)
        elif command == "/queue_status":
            queue_status_command(tg, actor)
        elif command == "/summary":
            show_summary(tg, actor)
        elif command == "/calendar":
            show_calendar(tg, actor)
        elif command == "/people":
            show_people(tg, actor)
        elif command == "/person":
            person_command(tg, actor, rest)
        elif command in {"/search", "/find"}:
            start_or_run_search(tg, actor, rest)
        elif command == "/daily_report":
            daily_report_command(tg, actor, rest)
        elif command == "/sync_sheets":
            sync_sheets_command(tg, actor)
        elif command == "/sync_youtube_metrics":
            sync_youtube_metrics_command(tg, actor)
        elif command == "/metrics_youtube_today":
            metrics_youtube_today_command(tg, actor)
        elif command == "/metrics_youtube_all":
            metrics_youtube_all_command(tg, actor)
        elif command == "/metrics_video":
            metrics_video_command(tg, actor, rest)
        elif command == "/resend_pending":
            resend_pending_command(tg, actor)
        elif command == "/reset_admin_queue":
            reset_admin_queue_command(tg, actor)
        elif command == "/return_missing_dates":
            return_missing_dates_command(tg, actor)
        elif command == "/jobs_status":
            jobs_status_command(tg, actor)
        elif command == "/retry_failed_jobs":
            retry_failed_jobs_command(tg, actor)
        elif command == "/add_person":
            add_person_command(tg, actor, rest)
        elif command == "/activate_person":
            set_person_active_command(tg, actor, rest, True)
        elif command == "/deactivate_person":
            set_person_active_command(tg, actor, rest, False)
        elif command == "/edit_video":
            edit_video_command(tg, actor, rest)
        else:
            tg.send_message(actor.chat_id, "Не знаю такую команду. Напишите /help.")
        return

    session = db.get_session(actor.tg_id)
    if session:
        handle_session_message(tg, actor, session["state"], session.get("data") or {}, text)
    else:
        tg.send_message(actor.chat_id, "Выберите действие через /start или начните новую заявку: /new_video.")


def handle_callback(callback: dict[str, Any]) -> None:
    actor = _actor_from_callback(callback)
    if not actor:
        return
    data = callback.get("data") or ""
    message = callback.get("message") or {}
    message_id = message.get("message_id")
    tg = TelegramClient()

    callback_id = str(callback.get("id") or "")
    if data.startswith("dash:"):
        handle_dashboard_callback(tg, actor, data, message_id, callback_id)
        return
    if data.startswith("person:"):
        handle_person_profile_callback(tg, actor, data, callback_id)
        return
    if data.startswith("daily:"):
        handle_daily_report_callback(tg, actor, data, callback_id)
        return
    if data.startswith("admq:"):
        handle_admin_queue_callback(tg, actor, data, message_id, callback_id)
        return
    if data.startswith("missingdate:"):
        handle_missing_date_callback(tg, actor, data, callback_id)
        return
    if data.startswith("jobretry:"):
        handle_retry_failed_jobs_callback(tg, actor, data, callback_id)
        return
    if data.startswith("adm:"):
        handle_stale_admin_callback(tg, actor, callback_id)
        return

    try:
        tg.answer_callback_query(callback_id)
    except Exception:
        pass

    if data == "cmd:new":
        start_new_video(tg, actor)
    elif data == "cmd:new_bigrecap":
        start_new_bigrecap(tg, actor)
    elif data == "cmd:add_znambo":
        start_add_znambo(tg, actor)
    elif data == "cmd:my":
        show_my_requests(tg, actor)
    elif data == "cmd:admin":
        show_admin(tg, actor)
    elif data == "cmd:queue_status":
        queue_status_command(tg, actor)
    elif data == "cmd:summary":
        show_summary(tg, actor)
    elif data == "cmd:resend_pending":
        resend_pending_command(tg, actor)
    elif data == "cmd:reset_admin_queue":
        reset_admin_queue_command(tg, actor)
    elif data == "cmd:calendar":
        show_calendar(tg, actor)
    elif data == "cmd:people":
        show_people(tg, actor)
    elif data == "cmd:help":
        send_help(tg, actor)
    elif data.startswith("adm:date:"):
        _, _, raw_video_id, raw_batch_id, raw_index = data.split(":", 4)
        show_admin_date_options(tg, actor, int(raw_video_id), int(raw_batch_id), int(raw_index), message_id)
    elif data.startswith("adm:setdate:"):
        _, _, raw_video_id, raw_batch_id, raw_index, preset = data.split(":", 5)
        set_admin_date_preset(
            tg,
            actor,
            int(raw_video_id),
            int(raw_batch_id),
            int(raw_index),
            preset,
            message_id,
        )
    elif data.startswith("adm:manualdate:"):
        _, _, raw_video_id, raw_batch_id, raw_index = data.split(":", 4)
        start_admin_manual_date(tg, actor, int(raw_video_id), int(raw_batch_id), int(raw_index))
    elif data.startswith("proj:"):
        _, project_code = data.split(":", 1)
        handle_project_pick(tg, actor, project_code)
    elif data == "newdate:manual":
        start_new_manual_date(tg, actor)
    elif data.startswith("newdate:"):
        _, preset = data.split(":", 1)
        handle_new_date(tg, actor, NEW_DATE_PRESETS.get(preset, ""))
    elif data == "znambo:date:manual":
        start_add_znambo_manual_date(tg, actor)
    elif data.startswith("znambo:date:"):
        _, _, preset = data.split(":", 2)
        handle_add_znambo_date(tg, actor, ADD_ZNAMBO_DATE_PRESETS.get(preset, ""))
    elif data.startswith("p:"):
        _, short_role, raw_person_id = data.split(":", 2)
        handle_person_pick(tg, actor, short_role, int(raw_person_id))
    elif data.startswith("pm:"):
        _, short_role = data.split(":", 1)
        ask_manual_person(tg, actor, short_role)
    elif data == "ms":
        handle_montage_same_as_author(tg, actor)
    elif data == "vy":
        ask_people(tg, actor, "voice", show_voice_decision=False)
    elif data == "vn":
        handle_voice_none(tg, actor)
    elif data == "bigrecap:add_vk":
        start_bigrecap_vk_input(tg, actor)
    elif data.startswith("skip:"):
        _, platform = data.split(":", 1)
        session = db.get_session(actor.tg_id)
        if session and str(session.get("state", "")).startswith("links:"):
            handle_add_links_message(tg, actor, session.get("data") or {}, platform, "Пропустить")
        else:
            handle_optional_link(tg, actor, platform, "Пропустить")
    elif data == "submit":
        submit_video(tg, actor)
    elif data == "edit":
        handle_preview_edit(tg, actor)
    elif data == "cancel":
        db.clear_session(actor.tg_id)
        tg.send_message(actor.chat_id, "Заявка отменена.")
    elif data.startswith("links:"):
        _, raw_video_id = data.split(":", 1)
        start_add_links(tg, actor, int(raw_video_id))
    elif data.startswith("revise:"):
        _, raw_video_id = data.split(":", 1)
        start_revision(tg, actor, int(raw_video_id))
    elif data.startswith("revdate:"):
        handle_missing_date_revision_callback(tg, actor, data)
    elif data.startswith("adm:open:"):
        _, _, raw_batch_id, raw_index = data.split(":", 3)
        show_queue_item(tg, actor, int(raw_batch_id), int(raw_index), message_id)
    elif data.startswith("adm:a:"):
        _, _, raw_video_id, raw_batch_id, raw_index = data.split(":", 4)
        approve_one(tg, actor, int(raw_video_id), int(raw_batch_id), int(raw_index), message_id)
    elif data.startswith("adm:r:"):
        _, _, raw_video_id, raw_batch_id, raw_index = data.split(":", 4)
        mark_video_status(
            tg,
            actor,
            int(raw_video_id),
            int(raw_batch_id),
            int(raw_index),
            "needs_revision",
            "Заявка возвращена на правку.",
            message_id,
        )
    elif data.startswith("adm:d:"):
        _, _, raw_video_id, raw_batch_id, raw_index = data.split(":", 4)
        mark_video_status(
            tg,
            actor,
            int(raw_video_id),
            int(raw_batch_id),
            int(raw_index),
            "duplicate",
            "Заявка помечена как дубль.",
            message_id,
        )
    elif data.startswith("adm:x:"):
        _, _, raw_video_id, raw_batch_id, raw_index = data.split(":", 4)
        mark_video_status(
            tg,
            actor,
            int(raw_video_id),
            int(raw_batch_id),
            int(raw_index),
            "deleted",
            "Заявка удалена из очереди.",
            message_id,
        )
    elif data.startswith("adm:clean:"):
        _, _, raw_batch_id = data.split(":", 2)
        approve_clean_batch(tg, actor, int(raw_batch_id), message_id)
    elif data.startswith("adm:sum:"):
        _, _, raw_batch_id = data.split(":", 2)
        send_batch_summary(tg, actor.chat_id, int(raw_batch_id), edit_message_id=message_id)
    else:
        tg.send_message(actor.chat_id, "Действие устарело. Откройте меню заново: /start.")


def send_help(tg: TelegramClient, actor: Actor) -> None:
    lines = [
        "Команды:",
        "/new_video — добавить Reels",
        "/new_bigrecap — добавить большой рекап",
        "/my_requests — мои заявки и дополнение ссылок",
        "Дата публикации обязательна при добавлении заявки.",
        "/chatid — показать ID текущего Telegram-чата",
        "/admin — очередь проверки",
        "/queue_status — статус очереди",
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
    ]
    if is_superadmin(actor.tg_id):
        lines.extend(
            [
                "",
                "Для суперадминов:",
                "/add_znambo — быстро добавить мой ролик",
                "/reset_admin_queue — сбросить и восстановить FIFO-очередь",
                "/retry_failed_jobs — повторить временно упавшие задания",
                "/add_person role name [tg_id] [@username]",
                "/activate_person id",
                "/deactivate_person id",
                "",
                "Роли: author, montage, voice, admin, superadmin.",
            ]
        )
    text = "\n".join(lines)
    tg.send_message(actor.chat_id, text)


def start_new_video(tg: TelegramClient, actor: Actor) -> None:
    start_new_submission(tg, actor, VIDEO_TYPE_REGULAR)


def start_new_bigrecap(tg: TelegramClient, actor: Actor) -> None:
    start_new_submission(tg, actor, VIDEO_TYPE_BIGRECAP)


def start_add_znambo(tg: TelegramClient, actor: Actor) -> None:
    if not is_superadmin(actor.tg_id):
        tg.send_message(actor.chat_id, ADD_ZNAMBO_UNAUTHORIZED_MESSAGE)
        return
    if actor.chat_type != "private":
        username = get_settings().bot_username or "rngn_reels_wc_bot"
        tg.send_message(actor.chat_id, f"Команда работает в личке с ботом. Открой @{username} и отправь /add_znambo.")
        return
    db.set_session(
        tg_id=actor.tg_id,
        chat_id=actor.chat_id,
        username=actor.username,
        state=ADD_ZNAMBO_SESSION_INSTAGRAM,
        data={
            "flow": "add_znambo",
            "step": "awaiting_znambo_instagram",
            "video_type": VIDEO_TYPE_REGULAR,
        },
    )
    tg.send_message(actor.chat_id, ADD_ZNAMBO_LINK_PROMPT)


def project_picker_keyboard() -> dict[str, Any]:
    buttons = [
        (f"{project['emoji']} {project['name']}", f"proj:{project['code']}")
        for project in PROJECTS
    ]
    rows = [buttons[index : index + 2] for index in range(0, len(buttons) - 1, 2)]
    rows.append([buttons[-1]])
    return inline_keyboard(rows)


def get_active_project(code: str) -> dict[str, Any] | None:
    return db.fetch_one(
        "SELECT id, code, name, emoji FROM projects WHERE code = %s AND is_active = true",
        (code,),
    )


def ask_submission_project(
    tg: TelegramClient,
    actor: Actor,
    data: dict[str, Any],
    *,
    znambo_flow: bool = False,
) -> None:
    state = ADD_ZNAMBO_SESSION_PROJECT if znambo_flow else "new:project"
    data["project_flow"] = "znambo" if znambo_flow else "new"
    db.set_session(
        tg_id=actor.tg_id,
        chat_id=actor.chat_id,
        username=actor.username,
        state=state,
        data=data,
    )
    tg.send_message(actor.chat_id, PROJECT_PROMPT, project_picker_keyboard())


def _continue_after_project(
    tg: TelegramClient,
    actor: Actor,
    data: dict[str, Any],
    *,
    znambo_flow: bool,
) -> None:
    if znambo_flow:
        data["step"] = "awaiting_znambo_publish_date"
        ask_add_znambo_date(tg, actor, data)
        return
    ask_submission_date(tg, actor, data)


def ask_submission_date(tg: TelegramClient, actor: Actor, data: dict[str, Any]) -> None:
    db.set_session(
        tg_id=actor.tg_id,
        chat_id=actor.chat_id,
        username=actor.username,
        state=NEW_DATE_SESSION,
        data=data,
    )
    tg.send_message(
        actor.chat_id,
        NEW_DATE_PROMPT,
        inline_keyboard(
            [
                [("Сегодня", "newdate:today"), ("Вчера", "newdate:yesterday")],
                [("Ввести вручную", "newdate:manual")],
            ]
        ),
    )


def start_new_manual_date(tg: TelegramClient, actor: Actor) -> None:
    session = db.get_session(actor.tg_id)
    if not session or session.get("state") != NEW_DATE_SESSION:
        tg.send_message(actor.chat_id, "Начни заявку заново: /new_video.")
        return
    db.set_session(
        tg_id=actor.tg_id,
        chat_id=actor.chat_id,
        username=actor.username,
        state=NEW_DATE_MANUAL_SESSION,
        data=session.get("data") or {},
    )
    tg.send_message(actor.chat_id, NEW_DATE_MANUAL_PROMPT)


def parse_new_submission_date(raw: str) -> date:
    try:
        return parse_publish_date(raw)
    except ValueError as exc:
        raise ValueError(NEW_DATE_INVALID_MESSAGE) from exc


def handle_new_date(tg: TelegramClient, actor: Actor, text: str) -> None:
    session = db.get_session(actor.tg_id)
    if not session or session.get("state") not in {NEW_DATE_SESSION, NEW_DATE_MANUAL_SESSION}:
        tg.send_message(actor.chat_id, "Начни заявку заново: /new_video.")
        return
    data = session.get("data") or {}
    try:
        publish_date = parse_new_submission_date(text)
    except ValueError:
        tg.send_message(actor.chat_id, NEW_DATE_INVALID_MESSAGE)
        return
    data["publish_date"] = publish_date.isoformat()
    db.set_session(
        tg_id=actor.tg_id,
        chat_id=actor.chat_id,
        username=actor.username,
        state="new:author",
        data=data,
    )
    ask_people(tg, actor, "author")


def handle_project_pick(tg: TelegramClient, actor: Actor, project_code: str) -> None:
    session = db.get_session(actor.tg_id)
    state = session.get("state") if session else None
    if state not in {"new:project", ADD_ZNAMBO_SESSION_PROJECT}:
        tg.send_message(actor.chat_id, "Начни заявку заново.")
        return
    data = session.get("data") or {}
    znambo_flow = state == ADD_ZNAMBO_SESSION_PROJECT
    if project_code == "other":
        db.set_session(
            tg_id=actor.tg_id,
            chat_id=actor.chat_id,
            username=actor.username,
            state=ADD_ZNAMBO_SESSION_PROJECT_OTHER if znambo_flow else "new:project_other",
            data=data,
        )
        tg.send_message(actor.chat_id, PROJECT_OTHER_PROMPT)
        return
    project = get_active_project(project_code)
    if not project:
        tg.send_message(actor.chat_id, "Проект недоступен. Выбери другой.", project_picker_keyboard())
        return
    data.update(
        {
            "project_id": int(project["id"]),
            "project_code": str(project["code"]),
            "project_name": str(project["name"]),
        }
    )
    _continue_after_project(tg, actor, data, znambo_flow=znambo_flow)


def handle_project_other_message(
    tg: TelegramClient,
    actor: Actor,
    state: str,
    data: dict[str, Any],
    text: str,
) -> None:
    project_name = normalize_custom_project_name(text)
    if not project_name:
        tg.send_message(actor.chat_id, PROJECT_OTHER_INVALID_MESSAGE)
        return
    data.update({"project_id": None, "project_code": "other", "project_name": project_name})
    _continue_after_project(
        tg,
        actor,
        data,
        znambo_flow=state == ADD_ZNAMBO_SESSION_PROJECT_OTHER,
    )


def handle_add_znambo_instagram(
    tg: TelegramClient,
    actor: Actor,
    data: dict[str, Any],
    text: str,
) -> None:
    if not is_superadmin(actor.tg_id):
        db.clear_session(actor.tg_id)
        tg.send_message(actor.chat_id, ADD_ZNAMBO_UNAUTHORIZED_MESSAGE)
        return
    try:
        link = normalize_instagram(text)
    except ValueError:
        tg.send_message(actor.chat_id, ADD_ZNAMBO_INVALID_LINK_MESSAGE)
        return

    duplicate = find_video_by_instagram_id(link.external_id or "")
    if duplicate:
        db.clear_session(actor.tg_id)
        tg.send_message(actor.chat_id, format_add_znambo_duplicate(duplicate))
        return

    data.update(
        {
            "flow": "add_znambo",
            "step": "awaiting_znambo_project",
            "video_type": VIDEO_TYPE_REGULAR,
            "instagram_url": link.url,
            "instagram_id": link.external_id,
        }
    )
    ask_submission_project(tg, actor, data, znambo_flow=True)


def ask_add_znambo_date(tg: TelegramClient, actor: Actor, data: dict[str, Any]) -> None:
    db.set_session(
        tg_id=actor.tg_id,
        chat_id=actor.chat_id,
        username=actor.username,
        state=ADD_ZNAMBO_SESSION_DATE,
        data=data,
    )
    tg.send_message(
        actor.chat_id,
        ADD_ZNAMBO_DATE_PROMPT,
        inline_keyboard(
            [
                [("Сегодня", "znambo:date:today"), ("Вчера", "znambo:date:yesterday")],
                [("Ввести вручную", "znambo:date:manual")],
            ]
        ),
    )


def start_add_znambo_manual_date(tg: TelegramClient, actor: Actor) -> None:
    if not is_superadmin(actor.tg_id):
        db.clear_session(actor.tg_id)
        tg.send_message(actor.chat_id, ADD_ZNAMBO_UNAUTHORIZED_MESSAGE)
        return
    session = db.get_session(actor.tg_id)
    if not session or session.get("state") != ADD_ZNAMBO_SESSION_DATE:
        tg.send_message(actor.chat_id, "Начни заново: /add_znambo.")
        return
    tg.send_message(actor.chat_id, ADD_ZNAMBO_MANUAL_DATE_PROMPT)


def parse_add_znambo_date(raw: str) -> date:
    try:
        return parse_publish_date(raw)
    except ValueError as exc:
        raise ValueError(ADD_ZNAMBO_INVALID_DATE_MESSAGE) from exc


def handle_add_znambo_date(tg: TelegramClient, actor: Actor, text: str) -> None:
    if not is_superadmin(actor.tg_id):
        db.clear_session(actor.tg_id)
        tg.send_message(actor.chat_id, ADD_ZNAMBO_UNAUTHORIZED_MESSAGE)
        return
    session = db.get_session(actor.tg_id)
    if not session or session.get("state") != ADD_ZNAMBO_SESSION_DATE:
        tg.send_message(actor.chat_id, "Начни заново: /add_znambo.")
        return
    data = session.get("data") or {}
    try:
        publish_date = parse_add_znambo_date(text)
    except ValueError as exc:
        tg.send_message(actor.chat_id, str(exc))
        return

    try:
        result = upsert_znambo_quick_video(actor, data, publish_date)
    except psycopg.errors.UniqueViolation:
        duplicate = find_video_by_instagram_id(data.get("instagram_id") or "")
        db.clear_session(actor.tg_id)
        if duplicate:
            tg.send_message(actor.chat_id, format_add_znambo_duplicate(duplicate))
        else:
            tg.send_message(actor.chat_id, "Похоже, ролик уже был добавлен. Проверь /search.")
        return
    except Exception as exc:
        db.clear_session(actor.tg_id)
        record_system_log(
            "znambo_quick_failed",
            "video",
            None,
            {"error": _safe_error(exc), "instagram_id": data.get("instagram_id")},
            actor,
        )
        tg.send_message(actor.chat_id, f"Не удалось добавить ролик: {_safe_error(exc)}")
        return

    duplicate = result.get("duplicate")
    if duplicate:
        db.clear_session(actor.tg_id)
        tg.send_message(actor.chat_id, format_add_znambo_duplicate(duplicate))
        return

    video = result["video"]
    db.clear_session(actor.tg_id)
    sync_znambo_quick_to_sheets(video, actor)
    tg.send_message(actor.chat_id, format_add_znambo_success(video))


def _format_ddmmyyyy(value: Any) -> str:
    if isinstance(value, datetime):
        return value.date().strftime("%d.%m.%Y")
    if isinstance(value, date):
        return value.strftime("%d.%m.%Y")
    text = str(value or "").strip()
    if not text:
        return "не указана"
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date().strftime("%d.%m.%Y")
    except ValueError:
        return text


def format_add_znambo_duplicate(video: dict[str, Any]) -> str:
    lines = ["Этот ролик уже есть в базе."]
    if video.get("id") is not None:
        lines.append(f"ID: {video['id']}")
    if video.get("status"):
        lines.append(f"Статус: {video['status']}")
    if video.get("publish_date"):
        lines.append(f"Дата: {_format_ddmmyyyy(video.get('publish_date'))}")
    return "\n".join(lines)


def format_add_znambo_success(video: dict[str, Any]) -> str:
    return "\n".join(
        [
            "✅ Ролик Знамбо добавлен",
            "",
            f"Проект: {video.get('project_name') or 'не указан'}",
            f"Дата: {_format_ddmmyyyy(video.get('publish_date'))}",
            f"Instagram: {video.get('instagram_url') or ''}",
            f"Автор: {person_display(ADD_ZNAMBO_NAME, ADD_ZNAMBO_USERNAME)}",
            f"Озвучка: {person_display(ADD_ZNAMBO_NAME, ADD_ZNAMBO_USERNAME)}",
            f"Монтажёр: {person_display(ADD_ZNAMBO_NAME, ADD_ZNAMBO_USERNAME)}",
            f"Статус: {video.get('status') or 'approved'}",
        ]
    )


def _find_instagram_video_for_quick(conn, instagram_id: str, *, deleted: bool) -> dict[str, Any] | None:
    if not instagram_id:
        return None
    status_clause = "v.status = 'deleted'" if deleted else "v.status <> 'deleted'"
    order_clause = "v.updated_at DESC, v.id DESC" if deleted else "v.created_at ASC, v.id ASC"
    with conn.cursor() as cur:
        cur.execute(
            VIDEO_SELECT
            + f"""
            WHERE v.instagram_id = %s
              AND {status_clause}
            ORDER BY {order_clause}
            LIMIT 1
            """,
            (instagram_id,),
        )
        return cur.fetchone()


def resolve_znambo_people(conn) -> dict[str, dict[str, Any]]:
    people: dict[str, dict[str, Any]] = {}
    for role in ("author", "montage", "voice"):
        sort_weight = ADD_ZNAMBO_SORT_WEIGHTS[role]
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id
                FROM people
                WHERE role = %s
                  AND (
                    lower(COALESCE(username, '')) = lower(%s)
                    OR lower(name) = lower(%s)
                  )
                ORDER BY
                    CASE WHEN lower(COALESCE(username, '')) = lower(%s) THEN 0 ELSE 1 END,
                    CASE WHEN lower(name) = lower(%s) THEN 0 ELSE 1 END,
                    CASE WHEN is_active THEN 0 ELSE 1 END,
                    sort_weight DESC,
                    id ASC
                LIMIT 1
                """,
                (role, ADD_ZNAMBO_USERNAME, ADD_ZNAMBO_NAME, ADD_ZNAMBO_USERNAME, ADD_ZNAMBO_NAME),
            )
            row = cur.fetchone()
            if row:
                cur.execute(
                    """
                    UPDATE people
                    SET name = %s,
                        username = %s,
                        is_active = true,
                        sort_weight = GREATEST(COALESCE(sort_weight, 0), %s)
                    WHERE id = %s
                    RETURNING id, name, username
                    """,
                    (ADD_ZNAMBO_NAME, ADD_ZNAMBO_USERNAME, sort_weight, row["id"]),
                )
                people[role] = cur.fetchone()
                continue
            cur.execute(
                """
                INSERT INTO people (name, username, role, is_active, sort_weight)
                VALUES (%s, %s, %s, true, %s)
                RETURNING id, name, username
                """,
                (ADD_ZNAMBO_NAME, ADD_ZNAMBO_USERNAME, role, sort_weight),
            )
            people[role] = cur.fetchone()
    return people


def upsert_znambo_quick_video(
    actor: Actor,
    data: dict[str, Any],
    publish_date: date,
) -> dict[str, Any]:
    instagram_id = data.get("instagram_id")
    instagram_url = data.get("instagram_url")
    if not instagram_id or not instagram_url:
        raise ValueError("add_znambo requires instagram_url and instagram_id")
    if not data.get("project_code") or not data.get("project_name"):
        raise ValueError("project is required")

    with db.transaction() as conn:
        active = _find_instagram_video_for_quick(conn, instagram_id, deleted=False)
        if active:
            return {"duplicate": active, "video": None, "restored": False}

        people = resolve_znambo_people(conn)
        author = people["author"]
        montage = people["montage"]
        voice = people["voice"]
        deleted = _find_instagram_video_for_quick(conn, instagram_id, deleted=True)
        old_batch_id = deleted.get("batch_id") if deleted else None
        batch_id = ensure_open_batch(conn, actor)

        with conn.cursor() as cur:
            if deleted:
                cur.execute(
                    """
                    UPDATE videos
                    SET status = 'approved',
                        sheet_sync_status = 'queued',
                        sheet_sync_error = NULL,
                        video_type = %s,
                        project_id = %s,
                        project_code = %s,
                        project_name = %s,
                        publish_date = %s,
                        instagram_url = %s,
                        instagram_id = %s,
                        youtube_url = NULL,
                        youtube_id = NULL,
                        youtube_views = NULL,
                        youtube_likes = NULL,
                        youtube_comments = NULL,
                        youtube_last_sync_at = NULL,
                        tiktok_url = NULL,
                        tiktok_id = NULL,
                        vk_url = NULL,
                        vk_id = NULL,
                        author_id = %s,
                        author_name = %s,
                        author_username = %s,
                        montage_id = %s,
                        montage_name = %s,
                        montage_username = %s,
                        montage_same_as_author = false,
                        voice_id = %s,
                        voice_name = %s,
                        voice_username = %s,
                        added_by_tg_id = %s,
                        added_by_username = %s,
                        checked_by_tg_id = %s,
                        checked_by_username = %s,
                        checked_at = now(),
                        publish_date_set_by_tg_id = %s,
                        publish_date_set_by_username = %s,
                        publish_date_set_at = now(),
                        admin_message_chat_id = NULL,
                        admin_message_id = NULL,
                        admin_notified_at = NULL,
                        batch_id = %s,
                        comment = NULL,
                        updated_at = now()
                    WHERE id = %s
                      AND status = 'deleted'
                    RETURNING id
                    """,
                    (
                        VIDEO_TYPE_REGULAR,
                        data.get("project_id"),
                        data.get("project_code"),
                        data.get("project_name"),
                        publish_date,
                        instagram_url,
                        instagram_id,
                        author["id"],
                        author["name"],
                        author.get("username"),
                        montage["id"],
                        montage["name"],
                        montage.get("username"),
                        voice["id"],
                        voice["name"],
                        voice.get("username"),
                        actor.tg_id,
                        actor.username,
                        actor.tg_id,
                        actor.username,
                        actor.tg_id,
                        actor.username,
                        batch_id,
                        deleted["id"],
                    ),
                )
                row = cur.fetchone()
                if not row:
                    active_after_race = _find_instagram_video_for_quick(conn, instagram_id, deleted=False)
                    if active_after_race:
                        return {"duplicate": active_after_race, "video": None, "restored": False}
                    raise RuntimeError("deleted video restore failed")
                video_id = int(row["id"])
                action = "znambo_quick_restored"
            else:
                cur.execute(
                    """
                    INSERT INTO videos (
                        status, video_type, project_id, project_code, project_name, publish_date,
                        instagram_url, instagram_id,
                        youtube_url, youtube_id, tiktok_url, tiktok_id, vk_url, vk_id,
                        author_id, author_name, author_username,
                        montage_id, montage_name, montage_username, montage_same_as_author,
                        voice_id, voice_name, voice_username,
                        added_by_tg_id, added_by_username,
                        checked_by_tg_id, checked_by_username, checked_at,
                        publish_date_set_by_tg_id, publish_date_set_by_username, publish_date_set_at,
                        batch_id, comment, sheet_sync_status
                    )
                    VALUES (
                        'approved', %s, %s, %s, %s, %s,
                        %s, %s,
                        NULL, NULL, NULL, NULL, NULL, NULL,
                        %s, %s, %s,
                        %s, %s, %s, false,
                        %s, %s, %s,
                        %s, %s,
                        %s, %s, now(),
                        %s, %s, now(),
                        %s, NULL, 'queued'
                    )
                    RETURNING id
                    """,
                    (
                        VIDEO_TYPE_REGULAR,
                        data.get("project_id"),
                        data.get("project_code"),
                        data.get("project_name"),
                        publish_date,
                        instagram_url,
                        instagram_id,
                        author["id"],
                        author["name"],
                        author.get("username"),
                        montage["id"],
                        montage["name"],
                        montage.get("username"),
                        voice["id"],
                        voice["name"],
                        voice.get("username"),
                        actor.tg_id,
                        actor.username,
                        actor.tg_id,
                        actor.username,
                        actor.tg_id,
                        actor.username,
                        batch_id,
                    ),
                )
                video_id = int(cur.fetchone()["id"])
                action = "znambo_quick_added"
            cur.execute("DELETE FROM admin_locks WHERE video_id = %s", (video_id,))

        if old_batch_id and int(old_batch_id) != int(batch_id):
            recalculate_batch(conn, int(old_batch_id))
        recalculate_batch(conn, int(batch_id))
        video = get_video_by_id(conn, video_id)
        db.log_event(
            conn,
            entity_type="video",
            entity_id=video_id,
            action=action,
            actor_tg_id=actor.tg_id,
            actor_username=actor.username,
            before_data={"status": "deleted", "batch_id": old_batch_id} if deleted else None,
            after_data={
                "status": "approved",
                "batch_id": batch_id,
                "video_type": VIDEO_TYPE_REGULAR,
                "publish_date": publish_date.isoformat(),
                "instagram_id": instagram_id,
                "project_code": data.get("project_code"),
            },
        )
        return {"duplicate": None, "video": video, "restored": bool(deleted)}


def sync_znambo_quick_to_sheets(video: dict[str, Any], actor: Actor) -> tuple[bool, str | None]:
    try:
        job_id = jobs.enqueue_sheet_sync(
            int(video["id"]),
            version=_sheet_sync_version(video),
        )
        record_system_log(
            "sheets_sync_queued",
            "video",
            int(video["id"]),
            {"flow": "add_znambo", "job_id": job_id},
            actor,
        )
        return job_id is not None, None if job_id is not None else "background jobs disabled"
    except Exception as exc:
        error = _safe_error(exc)
        record_system_log(
            "sheets_sync_queue_failed",
            "video",
            int(video["id"]),
            {"flow": "add_znambo", "error": error},
            actor,
        )
        return False, error


def start_new_submission(tg: TelegramClient, actor: Actor, video_type: str) -> None:
    normalized_type = normalize_video_type(video_type)
    if actor.chat_type != "private":
        username = get_settings().bot_username or "rngn_reels_wc_bot"
        tg.send_message(
            actor.chat_id,
            f"Видео нужно добавлять в личке с ботом.\nОткрой @{username} и нажми «Добавить ролик» или «Добавить большой рекап».",
        )
        return

    if normalized_type == VIDEO_TYPE_BIGRECAP:
        db.set_session(
            tg_id=actor.tg_id,
            chat_id=actor.chat_id,
            username=actor.username,
            state="new:bigrecap_youtube",
            data={
                "video_type": VIDEO_TYPE_BIGRECAP,
                "platform_flow": PLATFORM_FLOW_BIGRECAP,
                "step": "awaiting_bigrecap_youtube",
            },
        )
        tg.send_message(actor.chat_id, BIGRECAP_YOUTUBE_PROMPT)
        return

    db.set_session(
        tg_id=actor.tg_id,
        chat_id=actor.chat_id,
        username=actor.username,
        state="new:instagram",
        data={
            "video_type": VIDEO_TYPE_REGULAR,
            "platform_flow": PLATFORM_FLOW_REGULAR,
        },
    )
    tg.send_message(actor.chat_id, "Пришлите Instagram/Reels ссылку.")


def handle_session_message(
    tg: TelegramClient,
    actor: Actor,
    state: str,
    data: dict[str, Any],
    text: str,
) -> None:
    if state == "new:instagram":
        handle_new_instagram(tg, actor, data, text)
    elif state == "new:bigrecap_youtube":
        handle_new_bigrecap_youtube(tg, actor, data, text)
    elif state == ADD_ZNAMBO_SESSION_INSTAGRAM:
        handle_add_znambo_instagram(tg, actor, data, text)
    elif state in {"new:project", ADD_ZNAMBO_SESSION_PROJECT}:
        tg.send_message(actor.chat_id, PROJECT_PROMPT, project_picker_keyboard())
    elif state in {"new:project_other", ADD_ZNAMBO_SESSION_PROJECT_OTHER}:
        handle_project_other_message(tg, actor, state, data, text)
    elif state in {NEW_DATE_SESSION, NEW_DATE_MANUAL_SESSION}:
        handle_new_date(tg, actor, text)
    elif state == ADD_ZNAMBO_SESSION_DATE:
        handle_add_znambo_date(tg, actor, text)
    elif state == "admin:date":
        handle_admin_date_message(tg, actor, data, text)
    elif state == "admin:project_other":
        handle_admin_project_other_message(tg, actor, data, text)
    elif state == "new:author_manual":
        handle_manual_person_value(tg, actor, "a", text)
    elif state == "new:voice_manual":
        handle_manual_person_value(tg, actor, "v", text)
    elif state == "new:montage_manual":
        handle_manual_person_value(tg, actor, "m", text)
    elif state == "new:youtube":
        handle_optional_link(tg, actor, "youtube", text)
    elif state == "new:tiktok":
        handle_optional_link(tg, actor, "tiktok", text)
    elif state == "new:vk":
        handle_optional_link(tg, actor, "vk", text)
    elif state == "new:bigrecap_vk_choice":
        tg.send_message(actor.chat_id, "Выберите действие кнопкой: добавить VK или пропустить.")
    elif state == "new:bigrecap_vk":
        handle_optional_link(tg, actor, "vk", text)
    elif state in {"search:query", "admin:search"}:
        db.clear_session(actor.tg_id)
        run_search(tg, actor, text)
    elif state == "admin:person":
        db.clear_session(actor.tg_id)
        person_command(tg, actor, text)
    elif state == "links:youtube":
        handle_add_links_message(tg, actor, data, "youtube", text)
    elif state == "links:tiktok":
        handle_add_links_message(tg, actor, data, "tiktok", text)
    elif state == "links:vk":
        handle_add_links_message(tg, actor, data, "vk", text)
    elif state == "revision:missing_date":
        handle_missing_date_revision_message(tg, actor, data, text)
    else:
        db.clear_session(actor.tg_id)
        tg.send_message(actor.chat_id, "Состояние формы устарело. Начните заново: /new_video.")


def handle_new_instagram(
    tg: TelegramClient,
    actor: Actor,
    data: dict[str, Any],
    text: str,
) -> None:
    try:
        link = normalize_instagram(text)
    except ValueError as exc:
        tg.send_message(actor.chat_id, str(exc))
        return

    duplicate = find_video_by_instagram_id(link.external_id or "")
    if duplicate:
        db.clear_session(actor.tg_id)
        tg.send_message(
            actor.chat_id,
            format_video_card(duplicate, title="Такое видео уже есть"),
        )
        return

    data.update({"instagram_url": link.url, "instagram_id": link.external_id})
    ask_submission_project(tg, actor, data)


def handle_new_bigrecap_youtube(
    tg: TelegramClient,
    actor: Actor,
    data: dict[str, Any],
    text: str,
) -> None:
    try:
        link = normalize_optional("youtube", text)
    except ValueError:
        link = None
    if not link or not link.external_id or not extract_youtube_id(link.url):
        tg.send_message(actor.chat_id, BIGRECAP_YOUTUBE_INVALID_MESSAGE)
        return

    duplicate = find_video_by_youtube_id(link.external_id)
    if duplicate:
        db.clear_session(actor.tg_id)
        tg.send_message(actor.chat_id, "Похоже, этот YouTube-ролик уже был добавлен.")
        tg.send_message(actor.chat_id, format_video_card(duplicate, title="Такое видео уже есть"))
        return

    data.update(
        {
            "video_type": VIDEO_TYPE_BIGRECAP,
            "platform_flow": PLATFORM_FLOW_BIGRECAP,
            "step": "bigrecap_youtube_ready",
            "instagram_url": None,
            "instagram_id": None,
            "youtube_url": link.url,
            "youtube_id": link.external_id,
            "tiktok_url": None,
            "tiktok_id": None,
        }
    )
    ask_submission_project(tg, actor, data)


def ask_people(
    tg: TelegramClient,
    actor: Actor,
    role: str,
    show_voice_decision: bool = True,
) -> None:
    people = get_people(role)
    short_role = SHORT_BY_ROLE[role]
    rows: list[list[tuple[str, str]]] = []
    if role == "voice":
        if show_voice_decision:
            tg.send_message(
                actor.chat_id,
                "Была ли в ролике озвучка другого автора?",
                inline_keyboard([[("Да, была", "vy"), ("Нет, не было", "vn")]]),
            )
            return
        rows.append([("Нет, не было", "vn")])
    if role == "montage":
        session = db.get_session(actor.tg_id)
        data = session.get("data") if session else {}
        if data and data.get("author_name"):
            rows.append([("Смонтировал сам автор", "ms")])
    for index in range(0, len(people), 2):
        row: list[tuple[str, str]] = []
        for person in people[index : index + 2]:
            row.append((person_display(person["name"], person.get("username")), f"p:{short_role}:{person['id']}"))
        rows.append(row)
    rows.append([("Нет в списке", f"pm:{short_role}")])
    label = {
        "author": "Выберите автора.",
        "voice": "Выберите автора озвучки.",
        "montage": "Выберите монтажёра.",
    }[role]
    tg.send_message(actor.chat_id, label, inline_keyboard(rows))


def get_people(role: str) -> list[dict[str, Any]]:
    usage_column = PERSON_USAGE_COLUMN[role]
    return db.fetch_all(
        f"""
        SELECT p.*, COALESCE(usage.count_used, 0) AS count_used
        FROM people p
        LEFT JOIN (
            SELECT {usage_column} AS person_id, count(*) AS count_used
            FROM videos
            WHERE {usage_column} IS NOT NULL
            GROUP BY {usage_column}
        ) usage ON usage.person_id = p.id
        WHERE p.role = %s AND p.is_active = true
        ORDER BY p.sort_weight DESC, count_used DESC, p.name ASC
        LIMIT 24
        """,
        (role,),
    )


def handle_person_pick(
    tg: TelegramClient,
    actor: Actor,
    short_role: str,
    person_id: int,
) -> None:
    role = ROLE_BY_SHORT.get(short_role)
    if not role:
        tg.send_message(actor.chat_id, "Неизвестная роль.")
        return
    session = db.get_session(actor.tg_id)
    if not session:
        tg.send_message(actor.chat_id, "Начните заявку заново: /new_video.")
        return
    person = db.fetch_one(
        "SELECT id, name, username FROM people WHERE id = %s AND role = %s AND is_active = true",
        (person_id, role),
    )
    if not person:
        tg.send_message(actor.chat_id, "Этого человека нет в активном списке.")
        return

    data = session.get("data") or {}
    data[f"{role}_id"] = person["id"]
    data[f"{role}_name"] = person["name"]
    data[f"{role}_username"] = person.get("username")
    if role == "montage":
        data["montage_same_as_author"] = False
    next_after_person(tg, actor, role, data)


def apply_montage_same_as_author(data: dict[str, Any]) -> dict[str, Any]:
    updated = dict(data)
    updated["montage_id"] = updated.get("author_id")
    updated["montage_name"] = updated.get("author_name")
    updated["montage_username"] = updated.get("author_username")
    updated["montage_same_as_author"] = True
    return updated


def handle_montage_same_as_author(tg: TelegramClient, actor: Actor) -> None:
    session = db.get_session(actor.tg_id)
    if not session:
        tg.send_message(actor.chat_id, "Начните заявку заново: /new_video.")
        return
    data = session.get("data") or {}
    if not data.get("author_name"):
        tg.send_message(actor.chat_id, "Сначала выберите автора.")
        return
    next_after_person(tg, actor, "montage", apply_montage_same_as_author(data))


def ask_manual_person(tg: TelegramClient, actor: Actor, short_role: str) -> None:
    role = ROLE_BY_SHORT.get(short_role)
    if not role:
        tg.send_message(actor.chat_id, "Неизвестная роль.")
        return
    state = {
        "author": "new:author_manual",
        "voice": "new:voice_manual",
        "montage": "new:montage_manual",
    }[role]
    session = db.get_session(actor.tg_id)
    data = session.get("data") if session else {}
    db.set_session(
        tg_id=actor.tg_id,
        chat_id=actor.chat_id,
        username=actor.username,
        state=state,
        data=data or {},
    )
    tg.send_message(actor.chat_id, "Введите имя вручную.")


def handle_manual_person_value(
    tg: TelegramClient,
    actor: Actor,
    short_role: str,
    text: str,
) -> None:
    role = ROLE_BY_SHORT[short_role]
    value = text.strip()
    if len(value) < 2:
        tg.send_message(actor.chat_id, "Имя слишком короткое. Введите ещё раз.")
        return
    session = db.get_session(actor.tg_id)
    if not session:
        tg.send_message(actor.chat_id, "Начните заявку заново: /new_video.")
        return
    data = session.get("data") or {}
    data[f"{role}_id"] = None
    data[f"{role}_name"] = value
    data[f"{role}_username"] = None
    if role == "montage":
        data["montage_same_as_author"] = False
    next_after_person(tg, actor, role, data)


def handle_voice_none(tg: TelegramClient, actor: Actor) -> None:
    session = db.get_session(actor.tg_id)
    if not session:
        tg.send_message(actor.chat_id, "Начните заявку заново: /new_video.")
        return
    data = session.get("data") or {}
    data["voice_id"] = None
    data["voice_name"] = None
    data["voice_username"] = None
    next_after_person(tg, actor, "voice", data)


def next_after_person(
    tg: TelegramClient,
    actor: Actor,
    role: str,
    data: dict[str, Any],
) -> None:
    if role == "author":
        db.set_session(
            tg_id=actor.tg_id,
            chat_id=actor.chat_id,
            username=actor.username,
            state="new:voice",
            data=data,
        )
        ask_people(tg, actor, "voice")
    elif role == "voice":
        db.set_session(
            tg_id=actor.tg_id,
            chat_id=actor.chat_id,
            username=actor.username,
            state="new:montage",
            data=data,
        )
        ask_people(tg, actor, "montage")
    else:
        if normalize_video_type(data.get("video_type")) == VIDEO_TYPE_BIGRECAP:
            ask_bigrecap_vk_choice(tg, actor, data)
            return
        db.set_session(
            tg_id=actor.tg_id,
            chat_id=actor.chat_id,
            username=actor.username,
            state="new:youtube",
            data=data,
        )
        tg.send_message(
            actor.chat_id,
            "Пришлите YouTube ссылку или пропустите.",
            inline_keyboard([[("Пропустить", "skip:youtube")]]),
        )


def ask_bigrecap_vk_choice(tg: TelegramClient, actor: Actor, data: dict[str, Any]) -> None:
    data["video_type"] = VIDEO_TYPE_BIGRECAP
    data["platform_flow"] = PLATFORM_FLOW_BIGRECAP
    data["step"] = "bigrecap_vk_choice"
    data["instagram_url"] = None
    data["instagram_id"] = None
    data["tiktok_url"] = None
    data["tiktok_id"] = None
    db.set_session(
        tg_id=actor.tg_id,
        chat_id=actor.chat_id,
        username=actor.username,
        state="new:bigrecap_vk_choice",
        data=data,
    )
    tg.send_message(
        actor.chat_id,
        "Добавить ссылку VK?",
        inline_keyboard(
            [
                [("Добавить VK", "bigrecap:add_vk")],
                [("Пропустить VK", "skip:vk")],
            ]
        ),
    )


def start_bigrecap_vk_input(tg: TelegramClient, actor: Actor) -> None:
    session = db.get_session(actor.tg_id)
    if not session:
        tg.send_message(actor.chat_id, "Начните заявку заново: /new_bigrecap.")
        return
    data = session.get("data") or {}
    data["video_type"] = VIDEO_TYPE_BIGRECAP
    data["platform_flow"] = PLATFORM_FLOW_BIGRECAP
    data["step"] = "awaiting_bigrecap_vk"
    db.set_session(
        tg_id=actor.tg_id,
        chat_id=actor.chat_id,
        username=actor.username,
        state="new:bigrecap_vk",
        data=data,
    )
    tg.send_message(actor.chat_id, "Пришли ссылку на VK")


def handle_optional_link(
    tg: TelegramClient,
    actor: Actor,
    platform: str,
    text: str,
) -> None:
    session = db.get_session(actor.tg_id)
    if not session:
        tg.send_message(actor.chat_id, "Начните заявку заново: /new_video.")
        return
    data = session.get("data") or {}
    try:
        link = normalize_optional(platform, text)
    except ValueError:
        tg.send_message(actor.chat_id, "Не удалось разобрать ссылку. Пришлите её ещё раз или нажмите «Пропустить».")
        return
    if link:
        data[f"{platform}_url"] = link.url
        data[f"{platform}_id"] = link.external_id

    if platform == "youtube":
        db.set_session(
            tg_id=actor.tg_id,
            chat_id=actor.chat_id,
            username=actor.username,
            state="new:tiktok",
            data=data,
        )
        tg.send_message(actor.chat_id, "Пришлите TikTok ссылку или пропустите.", inline_keyboard([[("Пропустить", "skip:tiktok")]]))
    elif platform == "tiktok":
        db.set_session(
            tg_id=actor.tg_id,
            chat_id=actor.chat_id,
            username=actor.username,
            state="new:vk",
            data=data,
        )
        tg.send_message(actor.chat_id, "Пришлите VK ссылку или пропустите.", inline_keyboard([[("Пропустить", "skip:vk")]]))
    else:
        show_new_preview(tg, actor, data)


def show_new_preview(tg: TelegramClient, actor: Actor, data: dict[str, Any]) -> None:
    data["video_type"] = normalize_video_type(data.get("video_type"))
    preview = {
        "id": data.get("edit_video_id") or "новая",
        "status": "draft",
        "added_by_tg_id": actor.tg_id,
        "added_by_username": actor.username,
        **data,
    }
    db.set_session(
        tg_id=actor.tg_id,
        chat_id=actor.chat_id,
        username=actor.username,
        state="new:preview",
        data=data,
    )
    tg.send_message(
        actor.chat_id,
        format_video_card(preview, title="Предпросмотр"),
        inline_keyboard(
            [
                [("Отправить на проверку", "submit")],
                [("Изменить", "edit"), ("Отменить", "cancel")],
            ]
        ),
    )


def handle_preview_edit(tg: TelegramClient, actor: Actor) -> None:
    session = db.get_session(actor.tg_id)
    if not session:
        tg.send_message(actor.chat_id, "Начните заявку заново: /new_video.")
        return
    data = session.get("data") or {}
    video_type = normalize_video_type(data.get("video_type"))
    keep = {
        "edit_video_id": data.get("edit_video_id"),
        "video_type": video_type,
        "platform_flow": PLATFORM_FLOW_BIGRECAP if video_type == VIDEO_TYPE_BIGRECAP else PLATFORM_FLOW_REGULAR,
        "project_id": data.get("project_id"),
        "project_code": data.get("project_code"),
        "project_name": data.get("project_name"),
        "publish_date": data.get("publish_date"),
    }
    if video_type == VIDEO_TYPE_BIGRECAP:
        keep.update(
            {
                "instagram_url": None,
                "instagram_id": None,
                "youtube_url": data.get("youtube_url"),
                "youtube_id": data.get("youtube_id"),
                "tiktok_url": None,
                "tiktok_id": None,
            }
        )
    else:
        keep.update(
            {
                "instagram_url": data.get("instagram_url"),
                "instagram_id": data.get("instagram_id"),
            }
        )
    db.set_session(
        tg_id=actor.tg_id,
        chat_id=actor.chat_id,
        username=actor.username,
        state="new:author",
        data=keep,
    )
    if video_type == VIDEO_TYPE_BIGRECAP:
        tg.send_message(actor.chat_id, "Ок, оставляю YouTube и пройдём поля заново.")
    else:
        tg.send_message(actor.chat_id, "Ок, оставляю Instagram и пройдём поля заново.")
    ask_people(tg, actor, "author")


def submit_video(tg: TelegramClient, actor: Actor) -> None:
    session = db.get_session(actor.tg_id)
    if not session:
        tg.send_message(actor.chat_id, "Начните заявку заново: /new_video.")
        return
    data = session.get("data") or {}
    data["video_type"] = normalize_video_type(data.get("video_type"))
    if data["video_type"] == VIDEO_TYPE_REGULAR and not data.get("instagram_id"):
        tg.send_message(actor.chat_id, "В заявке нет Instagram ID. Начните заново: /new_video.")
        return
    if data["video_type"] == VIDEO_TYPE_BIGRECAP and not data.get("youtube_id"):
        tg.send_message(actor.chat_id, "В заявке нет YouTube ID. Начните заново: /new_bigrecap.")
        return

    edit_video_id = int(data["edit_video_id"]) if data.get("edit_video_id") else None
    duplicate = (
        find_video_by_youtube_id(data["youtube_id"])
        if data["video_type"] == VIDEO_TYPE_BIGRECAP
        else find_video_by_instagram_id(data["instagram_id"])
    )
    if duplicate and duplicate.get("id") != edit_video_id:
        db.clear_session(actor.tg_id)
        if data["video_type"] == VIDEO_TYPE_BIGRECAP:
            tg.send_message(actor.chat_id, "Похоже, этот YouTube-ролик уже был добавлен.")
        tg.send_message(actor.chat_id, format_video_card(duplicate, title="Такое видео уже есть"))
        return

    try:
        if edit_video_id:
            video = update_revision_video(actor, edit_video_id, data)
        else:
            video = insert_pending_video(actor, data)
    except psycopg.errors.UniqueViolation:
        duplicate = (
            find_video_by_youtube_id(data["youtube_id"])
            if data["video_type"] == VIDEO_TYPE_BIGRECAP
            else find_video_by_instagram_id(data["instagram_id"])
        )
        db.clear_session(actor.tg_id)
        if duplicate:
            tg.send_message(actor.chat_id, format_video_card(duplicate, title="Такое видео уже есть"))
        else:
            tg.send_message(actor.chat_id, "Похоже, заявка уже была добавлена. Проверьте /my_requests.")
        return
    except ValueError as exc:
        db.clear_session(actor.tg_id)
        tg.send_message(actor.chat_id, _safe_error(exc))
        return
    except RuntimeError as exc:
        db.clear_session(actor.tg_id)
        tg.send_message(actor.chat_id, _safe_error(exc))
        return

    db.clear_session(actor.tg_id)
    tg.send_message(actor.chat_id, "Заявка отправлена на проверку.")
    if not notify_admin_queue(tg, video, actor):
        tg.send_message(
            actor.chat_id,
            "Заявка создана, но бот не смог отправить её в админский чат.\n"
            "Админу нужно проверить ADMIN_CHAT_ID или вызвать /resend_pending после исправления.",
        )


def normalized_submission_data(data: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(data)
    video_type = normalize_video_type(normalized.get("video_type"))
    normalized["video_type"] = video_type
    if not normalized.get("project_code") or not normalized.get("project_name"):
        raise ValueError("project is required")
    if not normalized.get("publish_date"):
        raise ValueError("publish_date is required")
    if video_type == VIDEO_TYPE_BIGRECAP:
        if not normalized.get("youtube_url") or not normalized.get("youtube_id"):
            raise ValueError("bigrecap requires youtube_url")
        normalized["platform_flow"] = PLATFORM_FLOW_BIGRECAP
        normalized["instagram_url"] = None
        normalized["instagram_id"] = None
        normalized["tiktok_url"] = None
        normalized["tiktok_id"] = None
    else:
        if not normalized.get("instagram_url") or not normalized.get("instagram_id"):
            raise ValueError("regular video requires instagram_url")
        normalized["platform_flow"] = PLATFORM_FLOW_REGULAR
    return normalized


def update_revision_video(actor: Actor, video_id: int, data: dict[str, Any]) -> dict[str, Any]:
    data = normalized_submission_data(data)
    with db.transaction() as conn:
        before = get_video_by_id(conn, video_id)
        if before.get("added_by_tg_id") != actor.tg_id and not is_admin(actor.tg_id):
            raise RuntimeError("revision is not owned by actor")
        batch_id = ensure_open_batch(conn, actor)
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE videos
                SET status = 'pending',
                    video_type = %s,
                    project_id = %s,
                    project_code = %s,
                    project_name = %s,
                    publish_date = COALESCE(%s, publish_date),
                    instagram_url = %s,
                    instagram_id = %s,
                    youtube_url = %s,
                    youtube_id = %s,
                    tiktok_url = %s,
                    tiktok_id = %s,
                    vk_url = %s,
                    vk_id = %s,
                    author_id = %s,
                    author_name = %s,
                    author_username = %s,
                    montage_id = %s,
                    montage_name = %s,
                    montage_username = %s,
                    montage_same_as_author = %s,
                    voice_id = %s,
                    voice_name = %s,
                    voice_username = %s,
                    checked_by_tg_id = NULL,
                    checked_by_username = NULL,
                    checked_at = NULL,
                    batch_id = %s,
                    updated_at = now()
                WHERE id = %s AND status = 'needs_revision'
                RETURNING id
                """,
                (
                    normalize_video_type(data.get("video_type")),
                    data.get("project_id"),
                    data.get("project_code"),
                    data.get("project_name"),
                    data.get("publish_date"),
                    data.get("instagram_url"),
                    data.get("instagram_id"),
                    data.get("youtube_url"),
                    data.get("youtube_id"),
                    data.get("tiktok_url"),
                    data.get("tiktok_id"),
                    data.get("vk_url"),
                    data.get("vk_id"),
                    data.get("author_id"),
                    data.get("author_name"),
                    data.get("author_username"),
                    data.get("montage_id"),
                    data.get("montage_name"),
                    data.get("montage_username"),
                    bool(data.get("montage_same_as_author")),
                    data.get("voice_id"),
                    data.get("voice_name"),
                    data.get("voice_username"),
                    batch_id,
                    video_id,
                ),
            )
            updated = cur.fetchone()
            if not updated:
                raise RuntimeError("revision is not available")
        recalculate_batch(conn, batch_id)
        db.log_event(
            conn,
            entity_type="video",
            entity_id=video_id,
            action="revision_submitted",
            actor_tg_id=actor.tg_id,
            actor_username=actor.username,
            before_data={"status": before.get("status")},
            after_data={
                "status": "pending",
                "batch_id": batch_id,
                "video_type": normalize_video_type(data.get("video_type")),
                "project_code": data.get("project_code"),
            },
        )
        return get_video_by_id(conn, video_id)


def insert_pending_video(actor: Actor, data: dict[str, Any]) -> dict[str, Any]:
    data = normalized_submission_data(data)
    with db.transaction() as conn:
        batch_id = ensure_open_batch(conn, actor)
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO videos (
                    status, video_type, project_id, project_code, project_name,
                    publish_date, instagram_url, instagram_id,
                    youtube_url, youtube_id, tiktok_url, tiktok_id, vk_url, vk_id,
                    author_id, author_name, author_username,
                    montage_id, montage_name, montage_username, montage_same_as_author,
                    voice_id, voice_name, voice_username,
                    added_by_tg_id, added_by_username, batch_id
                )
                VALUES (
                    'pending', %s, %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s, %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s
                )
                RETURNING id
                """,
                (
                    normalize_video_type(data.get("video_type")),
                    data.get("project_id"),
                    data.get("project_code"),
                    data.get("project_name"),
                    data.get("publish_date"),
                    data.get("instagram_url"),
                    data.get("instagram_id"),
                    data.get("youtube_url"),
                    data.get("youtube_id"),
                    data.get("tiktok_url"),
                    data.get("tiktok_id"),
                    data.get("vk_url"),
                    data.get("vk_id"),
                    data.get("author_id"),
                    data.get("author_name"),
                    data.get("author_username"),
                    data.get("montage_id"),
                    data.get("montage_name"),
                    data.get("montage_username"),
                    bool(data.get("montage_same_as_author")),
                    data.get("voice_id"),
                    data.get("voice_name"),
                    data.get("voice_username"),
                    actor.tg_id,
                    actor.username,
                    batch_id,
                ),
            )
            video_id = cur.fetchone()["id"]
        recalculate_batch(conn, batch_id)
        db.log_event(
            conn,
            entity_type="video",
            entity_id=video_id,
            action="submitted",
            actor_tg_id=actor.tg_id,
            actor_username=actor.username,
            after_data={
                "batch_id": batch_id,
                "video_type": normalize_video_type(data.get("video_type")),
                "project_code": data.get("project_code"),
            },
        )
        return get_video_by_id(conn, video_id)


def ensure_open_batch(conn, actor: Actor) -> int:
    settings = get_settings()
    minutes = int(settings.batch_window_minutes)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT id
            FROM batches
            WHERE status = 'open'
              AND updated_at > now() - interval '{minutes} minutes'
            ORDER BY updated_at DESC
            LIMIT 1
            """
        )
        row = cur.fetchone()
        if row:
            batch_id = int(row["id"])
            cur.execute("UPDATE batches SET updated_at = now() WHERE id = %s", (batch_id,))
            return batch_id
        cur.execute(
            """
            INSERT INTO batches (created_by_tg_id, created_by_username)
            VALUES (%s, %s)
            RETURNING id
            """,
            (actor.tg_id, actor.username),
        )
        return int(cur.fetchone()["id"])


def recalculate_batch(conn, batch_id: int) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                count(*) FILTER (WHERE status <> 'deleted') AS total_count,
                count(*) FILTER (
                    WHERE status = 'pending'
                      AND publish_date IS NOT NULL
                      AND COALESCE(project_code, '') <> ''
                      AND COALESCE(project_name, '') <> ''
                      AND (
                        (COALESCE(video_type, 'regular') = 'regular' AND instagram_id IS NOT NULL)
                        OR (COALESCE(video_type, 'regular') = 'bigrecap' AND youtube_id IS NOT NULL)
                      )
                      AND COALESCE(author_name, '') <> ''
                      AND COALESCE(montage_name, '') <> ''
                ) AS clean_count,
                count(*) FILTER (WHERE status = 'duplicate') AS duplicate_count,
                count(*) FILTER (
                    WHERE status = 'pending'
                      AND (
                        publish_date IS NULL
                        OR COALESCE(project_code, '') = ''
                        OR COALESCE(project_name, '') = ''
                        OR (
                          COALESCE(video_type, 'regular') = 'regular'
                          AND instagram_id IS NULL
                        )
                        OR (
                          COALESCE(video_type, 'regular') = 'bigrecap'
                          AND youtube_id IS NULL
                        )
                        OR COALESCE(author_name, '') = ''
                        OR COALESCE(montage_name, '') = ''
                      )
                ) AS problem_count
            FROM videos
            WHERE batch_id = %s
            """,
            (batch_id,),
        )
        counts = cur.fetchone()
        cur.execute(
            """
            UPDATE batches
            SET total_count = %s,
                clean_count = %s,
                duplicate_count = %s,
                problem_count = %s,
                updated_at = now()
            WHERE id = %s
            RETURNING *
            """,
            (
                counts["total_count"],
                counts["clean_count"],
                counts["duplicate_count"],
                counts["problem_count"],
                batch_id,
            ),
        )
        return cur.fetchone()


def notify_admin_queue(
    tg: TelegramClient,
    video: dict[str, Any],
    actor: Actor | None = None,
) -> bool:
    if not jobs.background_jobs_enabled():
        _safe_refresh_admin_dashboard(tg, actor, immediate=True)
        try:
            pump_admin_queue(tg, actor)
            return True
        except Exception:
            return False
    try:
        with db.transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT active_video_id FROM admin_queue_state WHERE queue_name = %s FOR UPDATE",
                    (ADMIN_QUEUE_NAME,),
                )
                state = cur.fetchone() or {}
            jobs.enqueue_dashboard_refresh(conn=conn)
            if not state.get("active_video_id"):
                jobs.enqueue_admin_queue_pump(conn=conn)
        return True
    except Exception as exc:
        record_system_log(
            "admin_queue_notify_failed",
            "video",
            int(video["id"]),
            telegram_failure_payload(exc, get_settings().admin_chat_id, "pump_after_submission"),
            actor,
        )
        return False


def send_admin_review_card(
    tg: TelegramClient,
    video: dict[str, Any],
    title: str = "Заявка",
    actor: Actor | None = None,
) -> bool:
    return notify_admin_queue(tg, video, actor)


def resend_pending_command(tg: TelegramClient, actor: Actor) -> None:
    if not require_admin(tg, actor):
        return
    _safe_refresh_admin_dashboard(tg, actor, immediate=True)
    result = pump_admin_queue(tg, actor, force_repost=True)
    record_system_log(
        "admin_queue_pumped",
        "admin_queue",
        result.get("active_video_id"),
        {"source": "resend_pending", **result},
        actor,
    )
    _safe_refresh_admin_dashboard(tg, actor)
    if result["pending_count"] == 0:
        tg.send_message(actor.chat_id, "Очередь пуста. Pending-заявок: 0.")


def queue_status_command(tg: TelegramClient, actor: Actor) -> None:
    if not require_admin(tg, actor):
        return
    with db.transaction() as conn:
        state = _queue_state_for_update(conn)
        snapshot = _admin_dashboard_snapshot(conn, state)
        dashboard_message_id = state.get("dashboard_message_id")
        filtered_count = _pending_video_count(conn, state)
    tg.send_message(
        actor.chat_id,
        "\n".join(
            [
                f"Pending: {snapshot['pending_count']}",
                f"Active: #{snapshot['active_video_id']}" if snapshot.get("active_video_id") else "Active: —",
                f"Dashboard message: {dashboard_message_id or '—'}",
                f"Filter: {_queue_filter_label(state)}",
                f"Filtered pending: {filtered_count}",
                f"Oldest: {_format_pending_age(snapshot.get('oldest_created_at'))}",
            ]
        ),
    )


def _dashboard_callback_is_current(actor: Actor, message_id: int | None) -> bool:
    try:
        state = db.fetch_one(
            """
            SELECT dashboard_chat_id, dashboard_message_id
            FROM admin_queue_state
            WHERE queue_name = %s
            """,
            (ADMIN_QUEUE_NAME,),
        )
    except Exception:
        return False
    return bool(
        state
        and message_id
        and int(state.get("dashboard_chat_id") or 0) == actor.chat_id
        and int(state.get("dashboard_message_id") or 0) == int(message_id)
    )


def _show_admin_queue_filters(tg: TelegramClient, actor: Actor) -> None:
    refresh_admin_dashboard(tg, actor)
    with db.transaction() as conn:
        state = _queue_state_for_update(conn)
        snapshot = _admin_dashboard_snapshot(conn, state)
        chat_id = int(state["dashboard_chat_id"])
        message_id = int(state["dashboard_message_id"])
    _edit_message_text_idempotent(
        tg,
        chat_id,
        message_id,
        "📂 ОЧЕРЕДЬ ПО ПРОЕКТАМ\n\nВыберите фильтр:",
        admin_queue_filter_keyboard(snapshot),
    )


def change_admin_queue_filter(
    tg: TelegramClient,
    actor: Actor,
    filter_type: str,
    filter_value: str | None = None,
) -> dict[str, Any]:
    if filter_type not in ADMIN_QUEUE_FILTER_TYPES:
        raise ValueError("unknown queue filter")
    if filter_type == "project":
        project = get_active_project(str(filter_value or ""))
        if not project or project["code"] == "other":
            raise ValueError("project filter is unavailable")
        filter_value = str(project["code"])
    else:
        filter_value = None

    archived: tuple[int, int, int] | None = None
    kept: tuple[int, int, dict[str, Any], int, int, str] | None = None
    with db.transaction() as conn:
        state = _queue_state_for_update(conn)
        before_type, before_value = _queue_filter(state)
        active_id = int(state["active_video_id"]) if state.get("active_video_id") else None
        active_chat_id = int(state["active_chat_id"]) if state.get("active_chat_id") else None
        active_message_id = int(state["active_message_id"]) if state.get("active_message_id") else None
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE admin_queue_state
                SET queue_filter_type = %s,
                    queue_filter_value = %s,
                    updated_at = now()
                WHERE queue_name = %s
                """,
                (filter_type, filter_value, ADMIN_QUEUE_NAME),
            )
        filtered_state = dict(state)
        filtered_state["queue_filter_type"] = filter_type
        filtered_state["queue_filter_value"] = filter_value
        target = _oldest_pending_video(conn, filtered_state)
        target_id = int(target["id"]) if target else None
        pointer_matches_target = bool(
            active_id
            and active_id == target_id
            and active_chat_id
            and active_message_id
            and active_chat_id == int(get_settings().admin_chat_id)
        )
        if pointer_matches_target and target:
            total = _pending_video_count(conn, filtered_state)
            position = _queue_position(conn, target, filtered_state)
            kept = (
                active_chat_id,
                active_message_id,
                target,
                total,
                position,
                _queue_filter_label(filtered_state),
            )
        else:
            if active_id and active_chat_id and active_message_id:
                archived = (active_chat_id, active_message_id, active_id)
            _clear_queue_state(conn)
        db.log_event(
            conn,
            entity_type="admin_queue",
            entity_id=active_id,
            action="admin_queue_filter_changed",
            actor_tg_id=actor.tg_id,
            actor_username=actor.username,
            before_data={"filter_type": before_type, "filter_value": before_value},
            after_data={
                "filter_type": filter_type,
                "filter_value": filter_value,
                "target_video_id": target_id,
            },
        )

    if archived:
        _archive_queue_message(
            tg,
            archived[0],
            archived[1],
            f"📂 Заявка #{archived[2]} скрыта после смены фильтра очереди.",
            actor,
        )
    if kept:
        _edit_message_text_idempotent(
            tg,
            kept[0],
            kept[1],
            format_admin_queue_card(kept[2], kept[3], kept[4], kept[5]),
            admin_queue_keyboard(int(kept[2]["id"])),
        )
    result = pump_admin_queue(tg, actor)
    _safe_refresh_admin_dashboard(tg, actor)
    return result


def handle_dashboard_callback(
    tg: TelegramClient,
    actor: Actor,
    data: str,
    message_id: int | None,
    callback_id: str,
) -> None:
    if not is_admin(actor.tg_id):
        _answer_queue_callback(tg, callback_id, "Это действие доступно только админам.", show_alert=True)
        return
    if not _dashboard_callback_is_current(actor, message_id):
        _answer_queue_callback(tg, callback_id, "Этот дашборд устарел. Откройте /admin.", show_alert=True)
        return
    action = data.split(":", 1)[1] if ":" in data else ""
    try:
        if action == "open":
            _safe_refresh_admin_dashboard(tg, actor, immediate=True)
            result = pump_admin_queue(tg, actor, force_repost=True)
            record_system_log(
                "admin_queue_pumped",
                "admin_queue",
                result.get("active_video_id"),
                {"source": "dashboard_open", **result},
                actor,
            )
            _safe_refresh_admin_dashboard(tg, actor, immediate=True)
        elif action == "projects":
            _show_admin_queue_filters(tg, actor)
        elif action == "people":
            start_person_lookup(tg, actor)
        elif action == "search":
            start_admin_search(tg, actor)
        elif action == "refresh":
            _safe_refresh_admin_dashboard(tg, actor, immediate=True)
        elif action.startswith("filter:"):
            parts = action.split(":")
            filter_type = parts[1] if len(parts) > 1 else ""
            filter_value = parts[2] if len(parts) > 2 else None
            change_admin_queue_filter(tg, actor, filter_type, filter_value)
        else:
            _answer_queue_callback(tg, callback_id, "Действие устарело.", show_alert=True)
            return
        _answer_queue_callback(tg, callback_id)
    except Exception as exc:
        record_system_log(
            "admin_queue_notify_failed",
            "admin_dashboard",
            None,
            {"source": f"dashboard_{action}", "error": _safe_error(exc)},
            actor,
        )
        _answer_queue_callback(tg, callback_id, "Не удалось обновить очередь.", show_alert=True)


def find_video_by_instagram_id(instagram_id: str) -> dict[str, Any] | None:
    if not instagram_id:
        return None
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                VIDEO_SELECT
                + """
                WHERE v.instagram_id = %s
                  AND v.status <> 'deleted'
                LIMIT 1
                """,
                (instagram_id,),
            )
            return cur.fetchone()


def find_video_by_youtube_id(youtube_id: str) -> dict[str, Any] | None:
    if not youtube_id:
        return None
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                VIDEO_SELECT
                + """
                WHERE v.youtube_id = %s
                  AND v.status <> 'deleted'
                LIMIT 1
                """,
                (youtube_id,),
            )
            return cur.fetchone()


def get_video_by_id(conn, video_id: int) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(VIDEO_SELECT + " WHERE v.id = %s", (video_id,))
        row = cur.fetchone()
        if not row:
            raise RuntimeError(f"Video {video_id} not found")
        return row


def get_video_by_id_outside(video_id: int) -> dict[str, Any] | None:
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(VIDEO_SELECT + " WHERE v.id = %s", (video_id,))
            return cur.fetchone()


def is_admin(tg_id: int) -> bool:
    return get_user_role(tg_id) in {"admin", "superadmin"}


def is_superadmin(tg_id: int) -> bool:
    return get_user_role(tg_id) == "superadmin"


def get_user_role(tg_id: int) -> str | None:
    if tg_id in get_settings().bootstrap_superadmin_ids:
        return "superadmin"
    try:
        row = db.fetch_one(
            """
            SELECT role
            FROM people
            WHERE tg_id = %s
              AND is_active = true
              AND role IN ('admin', 'superadmin')
            ORDER BY CASE role WHEN 'superadmin' THEN 0 ELSE 1 END
            LIMIT 1
            """,
            (tg_id,),
        )
    except Exception:
        return None
    return row["role"] if row else None


def require_admin(tg: TelegramClient, actor: Actor) -> bool:
    if not is_admin(actor.tg_id):
        tg.send_message(actor.chat_id, "Это действие доступно только админам.")
        return False
    return True


def require_superadmin(tg: TelegramClient, actor: Actor) -> bool:
    if not is_superadmin(actor.tg_id):
        tg.send_message(actor.chat_id, "Это действие доступно только суперадминам.")
        return False
    return True


def _queue_state_for_update(conn) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO admin_queue_state (queue_name) VALUES (%s) ON CONFLICT (queue_name) DO NOTHING",
            (ADMIN_QUEUE_NAME,),
        )
        cur.execute(
            "SELECT * FROM admin_queue_state WHERE queue_name = %s FOR UPDATE",
            (ADMIN_QUEUE_NAME,),
        )
        state = cur.fetchone()
    if not state:
        raise RuntimeError("Admin queue state is unavailable")
    return state


def _clear_queue_state(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE admin_queue_state
            SET active_video_id = NULL,
                active_chat_id = NULL,
                active_message_id = NULL,
                claimed_by_tg_id = NULL,
                claimed_by_username = NULL,
                claimed_at = NULL,
                updated_at = now()
            WHERE queue_name = %s
            """,
            (ADMIN_QUEUE_NAME,),
        )


def _queue_filter(state: dict[str, Any] | None) -> tuple[str, str | None]:
    filter_type = str((state or {}).get("queue_filter_type") or "global")
    filter_value = (state or {}).get("queue_filter_value")
    if filter_type not in ADMIN_QUEUE_FILTER_TYPES:
        return "global", None
    if filter_type == "project" and not filter_value:
        return "global", None
    return filter_type, str(filter_value) if filter_value else None


def _queue_filter_sql(
    state: dict[str, Any] | None,
    *,
    alias: str = "v",
) -> tuple[str, tuple[Any, ...]]:
    filter_type, filter_value = _queue_filter(state)
    if filter_type == "project":
        return f"{alias}.project_code = %s", (filter_value,)
    if filter_type == "other":
        return f"{alias}.project_code = 'other'", ()
    if filter_type == "unassigned":
        return (
            f"(COALESCE({alias}.project_code, '') = '' OR COALESCE({alias}.project_name, '') = '')",
            (),
        )
    return "TRUE", ()


def _queue_filter_label(state: dict[str, Any] | None) -> str:
    filter_type, filter_value = _queue_filter(state)
    if filter_type == "project":
        project = next((item for item in PROJECTS if item["code"] == filter_value), None)
        return str(project["name"]) if project else str(filter_value)
    if filter_type == "other":
        return "Другие проекты"
    if filter_type == "unassigned":
        return "Без проекта"
    return "Все проекты"


def _video_matches_queue_filter(video: dict[str, Any], state: dict[str, Any] | None) -> bool:
    filter_type, filter_value = _queue_filter(state)
    project_code = str(video.get("project_code") or "")
    project_name = str(video.get("project_name") or "")
    if filter_type == "project":
        return project_code == filter_value
    if filter_type == "other":
        return project_code == "other"
    if filter_type == "unassigned":
        return not project_code or not project_name
    return True


def _pending_video_count(conn, state: dict[str, Any] | None = None) -> int:
    condition, params = _queue_filter_sql(state)
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT count(*) AS count FROM videos v WHERE v.status = 'pending' AND {condition}",
            params,
        )
        return int(cur.fetchone()["count"])


def _format_pending_age(value: Any, now: datetime | None = None) -> str:
    if not isinstance(value, datetime):
        return "—"
    current = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    seconds = max(0, int((current - value.astimezone(timezone.utc)).total_seconds()))
    days, remainder = divmod(seconds, 86400)
    hours = remainder // 3600
    return f"{days} дн. {hours} ч." if days else f"{hours} ч."


def _admin_dashboard_snapshot(conn, state: dict[str, Any] | None = None) -> dict[str, Any]:
    queue_state = state or _queue_state_for_update(conn)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*) AS pending_count, min(created_at) AS oldest_created_at
            FROM videos
            WHERE status = 'pending'
            """
        )
        totals = cur.fetchone()
        cur.execute(
            """
            SELECT p.code, p.name, p.emoji, p.sort_order, count(v.id) AS count
            FROM projects p
            LEFT JOIN videos v
              ON v.project_code = p.code
             AND v.status = 'pending'
            WHERE p.is_active = true
              AND p.code <> 'other'
            GROUP BY p.code, p.name, p.emoji, p.sort_order
            ORDER BY p.sort_order, p.name
            """
        )
        permanent = list(cur.fetchall())
        cur.execute(
            """
            SELECT project_name AS name, count(*) AS count
            FROM videos
            WHERE status = 'pending'
              AND project_code = 'other'
              AND COALESCE(project_name, '') <> ''
            GROUP BY project_name
            ORDER BY project_name
            """
        )
        custom = list(cur.fetchall())
        cur.execute(
            """
            SELECT count(*) AS count
            FROM videos
            WHERE status = 'pending'
              AND (COALESCE(project_code, '') = '' OR COALESCE(project_name, '') = '')
            """
        )
        unassigned = int(cur.fetchone()["count"])
    project_counts = [
        {
            "code": row["code"],
            "name": row["name"],
            "emoji": row.get("emoji") or "📂",
            "count": int(row["count"]),
        }
        for row in permanent
    ]
    project_counts.extend(
        {"code": "other", "name": row["name"], "emoji": "➕", "count": int(row["count"])}
        for row in custom
    )
    project_counts.append({"code": "unassigned", "name": "Без проекта", "emoji": "❓", "count": unassigned})
    return {
        "pending_count": int(totals["pending_count"]),
        "active_video_id": queue_state.get("active_video_id"),
        "oldest_created_at": totals.get("oldest_created_at"),
        "project_counts": project_counts,
        "updated_at": datetime.now(get_settings().tz),
        "queue_filter_type": _queue_filter(queue_state)[0],
        "queue_filter_value": _queue_filter(queue_state)[1],
        "queue_filter_label": _queue_filter_label(queue_state),
    }


def format_admin_dashboard(snapshot: dict[str, Any]) -> str:
    pending_count = int(snapshot.get("pending_count") or 0)
    active_id = snapshot.get("active_video_id")
    lines = ["📊 ОЧЕРЕДЬ РИЛЗОВ", ""]
    if pending_count == 0:
        lines.append("🟢 Очередь разобрана")
    else:
        lines.extend(
            [
                f"🔴 Ждут проверки: {pending_count}",
                f"▶️ Текущая заявка: #{active_id}" if active_id else "▶️ Текущая заявка: не назначена",
                f"⏳ Самая старая: {_format_pending_age(snapshot.get('oldest_created_at'))}",
                "",
                "По проектам:",
            ]
        )
        lines.extend(
            f"{row.get('emoji') or '📂'} {row['name']} — {int(row.get('count') or 0)}"
            for row in snapshot.get("project_counts") or []
        )
    updated_at = snapshot.get("updated_at")
    updated_text = updated_at.strftime("%H:%M") if isinstance(updated_at, datetime) else "—"
    lines.extend(["", f"Обновлено: {updated_text}"])
    return "\n".join(lines)


def admin_dashboard_keyboard() -> dict[str, Any]:
    return inline_keyboard(
        [
            [("▶️ Открыть текущую", "dash:open")],
            [("📂 Очередь по проектам", "dash:projects")],
            [("👥 Участники", "dash:people"), ("🔎 Поиск", "dash:search")],
            [("🔄 Обновить", "dash:refresh")],
        ]
    )


def admin_queue_filter_keyboard(snapshot: dict[str, Any]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for row in snapshot.get("project_counts") or []:
        code = str(row.get("code") or "unassigned")
        counts[code] = counts.get(code, 0) + int(row.get("count") or 0)
    current_type = str(snapshot.get("queue_filter_type") or "global")
    current_value = snapshot.get("queue_filter_value")

    def label(text: str, selected: bool) -> str:
        return f"✓ {text}" if selected else text

    rows: list[list[tuple[str, str]]] = [
        [
            (
                label(f"🌐 Все проекты — {int(snapshot.get('pending_count') or 0)}", current_type == "global"),
                "dash:filter:global",
            )
        ]
    ]
    for project in PROJECTS:
        if project["code"] == "other":
            continue
        code = str(project["code"])
        rows.append(
            [
                (
                    label(
                        f"{project['emoji']} {project['name']} — {counts.get(code, 0)}",
                        current_type == "project" and current_value == code,
                    ),
                    f"dash:filter:project:{code}",
                )
            ]
        )
    rows.extend(
        [
            [
                (
                    label(f"➕ Другие проекты — {counts.get('other', 0)}", current_type == "other"),
                    "dash:filter:other",
                )
            ],
            [
                (
                    label(f"❓ Без проекта — {counts.get('unassigned', 0)}", current_type == "unassigned"),
                    "dash:filter:unassigned",
                )
            ],
            [("↩️ Назад", "dash:refresh")],
        ]
    )
    return inline_keyboard(rows)


def _dashboard_message_missing(exc: TelegramAPIError) -> bool:
    description = exc.description.lower()
    return any(
        marker in description
        for marker in ("message to edit not found", "message can't be edited", "message not found")
    )


def refresh_admin_dashboard(
    tg: TelegramClient,
    actor: Actor | None = None,
) -> dict[str, Any]:
    created = False
    chat_id = int(get_settings().admin_chat_id)
    message_id: int | None = None
    with db.transaction() as conn:
        state = _queue_state_for_update(conn)
        snapshot = _admin_dashboard_snapshot(conn, state)
        text = format_admin_dashboard(snapshot)
        stored_message_id = int(state["dashboard_message_id"]) if state.get("dashboard_message_id") else None
        stored_chat_id = int(state["dashboard_chat_id"]) if state.get("dashboard_chat_id") else None
        if stored_message_id and stored_chat_id == chat_id:
            try:
                _edit_message_text_idempotent(
                    tg,
                    chat_id,
                    stored_message_id,
                    text,
                    admin_dashboard_keyboard(),
                )
                message_id = stored_message_id
            except TelegramAPIError as exc:
                if not _dashboard_message_missing(exc):
                    raise
        if message_id is None:
            response = tg.send_message(chat_id, text, admin_dashboard_keyboard())
            message_id = _message_id(response)
            if not message_id:
                raise RuntimeError("Telegram did not return a message_id for the admin dashboard")
            created = True
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE admin_queue_state
                SET dashboard_chat_id = %s,
                    dashboard_message_id = %s,
                    dashboard_updated_at = now(),
                    updated_at = now()
                WHERE queue_name = %s
                """,
                (chat_id, message_id, ADMIN_QUEUE_NAME),
            )
        db.log_event(
            conn,
            entity_type="admin_dashboard",
            entity_id=message_id,
            action="admin_dashboard_refreshed",
            actor_tg_id=actor.tg_id if actor else None,
            actor_username=actor.username if actor else None,
            after_data={
                "pending_count": snapshot["pending_count"],
                "active_video_id": snapshot.get("active_video_id"),
                "created": created,
            },
        )
    pin_ok: bool | None = None
    if created:
        try:
            tg.pin_chat_message(chat_id, message_id, disable_notification=True)
            pin_ok = True
        except Exception as exc:
            pin_ok = False
            record_system_log(
                "admin_dashboard_pin_failed",
                "telegram_message",
                message_id,
                telegram_failure_payload(exc, chat_id, "pin_dashboard"),
                actor,
            )
    return {
        "message_id": message_id,
        "created": created,
        "pin_ok": pin_ok,
        **snapshot,
    }


def _safe_refresh_admin_dashboard(
    tg: TelegramClient,
    actor: Actor | None = None,
    *,
    immediate: bool = False,
) -> dict[str, Any] | None:
    if jobs.background_jobs_enabled() and not immediate:
        try:
            job_id = jobs.enqueue_dashboard_refresh()
            return {"queued": True, "job_id": job_id}
        except Exception as exc:
            record_system_log(
                "admin_dashboard_queue_failed",
                "admin_dashboard",
                None,
                {"error": _safe_error(exc)},
                actor,
            )
    try:
        return refresh_admin_dashboard(tg, actor)
    except Exception as exc:
        record_system_log(
            "admin_dashboard_refresh_failed",
            "admin_dashboard",
            None,
            telegram_failure_payload(exc, get_settings().admin_chat_id, "refresh_dashboard"),
            actor,
        )
        return None


def _oldest_pending_video(conn, state: dict[str, Any] | None = None) -> dict[str, Any] | None:
    condition, params = _queue_filter_sql(state)
    with conn.cursor() as cur:
        cur.execute(
            VIDEO_SELECT
            + f"""
            WHERE v.status = 'pending' AND {condition}
            ORDER BY v.created_at ASC, v.id ASC
            LIMIT 1
            """,
            params,
        )
        return cur.fetchone()


def _queue_position(conn, video: dict[str, Any], state: dict[str, Any] | None = None) -> int:
    condition, filter_params = _queue_filter_sql(state, alias="videos")
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT count(*) + 1 AS position
            FROM videos
            WHERE status = 'pending'
              AND {condition}
              AND (created_at, id) < (%s, %s)
            """,
            (*filter_params, video["created_at"], video["id"]),
        )
        return int(cur.fetchone()["position"])


def _format_admin_created_at(value: Any) -> str:
    if not value:
        return "не указано"
    if isinstance(value, datetime):
        return value.astimezone(get_settings().tz).strftime("%d.%m.%Y %H:%M")
    return str(value)


def _date_iso(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value or "").strip()
    return text or None


def format_admin_queue_card(
    video: dict[str, Any],
    total: int,
    position: int = 1,
    queue_label: str = "Все проекты",
) -> str:
    platforms = (
        (("YouTube", "youtube_url"), ("VK", "vk_url"))
        if normalize_video_type(video.get("video_type")) == VIDEO_TYPE_BIGRECAP
        else (
            ("Instagram", "instagram_url"),
            ("YouTube", "youtube_url"),
            ("TikTok", "tiktok_url"),
            ("VK", "vk_url"),
        )
    )
    link_lines = [f"{label}: {video[key]}" for label, key in platforms if video.get(key)]
    voice = person_value(video, "voice") if video.get("voice_name") else "нет"
    video_type = "большой рекап" if normalize_video_type(video.get("video_type")) == VIDEO_TYPE_BIGRECAP else "ролик"
    lines = [
        f"Заявка #{video['id']}",
        f"Очередь: {queue_label}",
        f"Позиция: {position} из {total}",
        f"Проект: {video.get('project_name') or 'не указан'}",
        f"Тип: {video_type}",
        "Статус: ожидает проверки",
        "",
        *link_lines,
        "",
        f"Дата публикации: {_format_ddmmyyyy(video.get('publish_date')) if video.get('publish_date') else 'не указана'}",
        "",
        f"Автор: {person_value(video, 'author')}",
        f"Монтажёр: {person_value(video, 'montage')}",
        f"Озвучка: {voice}",
        f"Добавил: {user_label(video.get('added_by_username'), video.get('added_by_tg_id'))}",
        f"Создано: {_format_admin_created_at(video.get('created_at'))}",
    ]
    if video.get("comment"):
        lines.append(f"Комментарий: {video['comment']}")
    return "\n".join(lines)


def admin_queue_keyboard(video_id: int) -> dict[str, Any]:
    return inline_keyboard(
        [
            [("Сменить проект", f"admq:project:{video_id}")],
            [("Указать дату", f"admq:date:{video_id}")],
            [("Одобрить", f"admq:approve:{video_id}"), ("Правка", f"admq:revision:{video_id}")],
            [("Дубль", f"admq:duplicate:{video_id}"), ("Удалить", f"admq:delete:{video_id}")],
            [("Обновить", f"admq:refresh:{video_id}")],
        ]
    )


def _edit_message_text_idempotent(
    tg: TelegramClient,
    chat_id: int,
    message_id: int,
    text: str,
    reply_markup: dict[str, Any],
) -> None:
    try:
        tg.edit_message_text(chat_id, message_id, text, reply_markup)
    except TelegramAPIError as exc:
        if "message is not modified" in exc.description.lower():
            return
        raise


def _archive_queue_message(
    tg: TelegramClient,
    chat_id: int | None,
    message_id: int | None,
    text: str,
    actor: Actor | None = None,
) -> None:
    if not chat_id or not message_id:
        return
    try:
        tg.edit_message_text(chat_id, message_id, text, {"inline_keyboard": []})
    except Exception as exc:
        record_system_log(
            "admin_queue_archive_failed",
            "telegram_message",
            message_id,
            telegram_failure_payload(exc, chat_id, "archive_queue_card"),
            actor,
        )


def _send_queue_card(
    tg: TelegramClient,
    conn,
    video: dict[str, Any],
    total: int,
    position: int,
    state: dict[str, Any],
) -> int:
    chat_id = int(get_settings().admin_chat_id)
    response = tg.send_message(
        chat_id,
        format_admin_queue_card(video, total, position, _queue_filter_label(state)),
        admin_queue_keyboard(int(video["id"])),
    )
    message_id = _message_id(response)
    if not message_id:
        raise RuntimeError("Telegram did not return a message_id for the admin queue card")
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE admin_queue_state
            SET active_video_id = %s,
                active_chat_id = %s,
                active_message_id = %s,
                claimed_by_tg_id = NULL,
                claimed_by_username = NULL,
                claimed_at = NULL,
                updated_at = now()
            WHERE queue_name = %s
            """,
            (video["id"], chat_id, message_id, ADMIN_QUEUE_NAME),
        )
        cur.execute(
            """
            UPDATE videos
            SET admin_message_chat_id = %s,
                admin_message_id = %s,
                admin_notified_at = now(),
                updated_at = now()
            WHERE id = %s
            """,
            (chat_id, message_id, video["id"]),
        )
    return message_id


def pump_admin_queue(
    tg: TelegramClient,
    actor: Actor | None = None,
    *,
    force_repost: bool = False,
) -> dict[str, Any]:
    with db.transaction() as conn:
        state = _queue_state_for_update(conn)
        total = _pending_video_count(conn, state)
        global_total = _pending_video_count(conn)
        active_id = int(state["active_video_id"]) if state.get("active_video_id") else None
        active_video = None
        if active_id:
            with conn.cursor() as cur:
                cur.execute("SELECT status FROM videos WHERE id = %s", (active_id,))
                active_status = cur.fetchone()
            pointer_is_complete = bool(
                state.get("active_chat_id")
                and state.get("active_message_id")
                and int(state["active_chat_id"]) == int(get_settings().admin_chat_id)
            )
            if active_status and active_status["status"] == "pending" and pointer_is_complete:
                active_video = get_video_by_id(conn, active_id)
                if not _video_matches_queue_filter(active_video, state):
                    active_video = None
                    _clear_queue_state(conn)
                elif not force_repost:
                    return {
                        "pending_count": total,
                        "global_pending_count": global_total,
                        "active_video_id": active_id,
                        "active_message_id": int(state["active_message_id"]),
                        "sent": False,
                        "queue_filter_type": _queue_filter(state)[0],
                        "queue_filter_value": _queue_filter(state)[1],
                    }
            else:
                _clear_queue_state(conn)
                active_id = None

        if total == 0:
            _clear_queue_state(conn)
            return {
                "pending_count": 0,
                "global_pending_count": global_total,
                "active_video_id": None,
                "active_message_id": None,
                "sent": False,
                "queue_filter_type": _queue_filter(state)[0],
                "queue_filter_value": _queue_filter(state)[1],
            }

        video = active_video or _oldest_pending_video(conn, state)
        if not video:
            _clear_queue_state(conn)
            return {
                "pending_count": total,
                "global_pending_count": global_total,
                "active_video_id": None,
                "active_message_id": None,
                "sent": False,
                "queue_filter_type": _queue_filter(state)[0],
                "queue_filter_value": _queue_filter(state)[1],
            }

        old_chat_id = int(state["active_chat_id"]) if active_video and state.get("active_chat_id") else None
        old_message_id = int(state["active_message_id"]) if active_video and state.get("active_message_id") else None
        position = _queue_position(conn, video, state)
        message_id = _send_queue_card(tg, conn, video, total, position, state)
        if old_chat_id and old_message_id and old_message_id != message_id:
            _archive_queue_message(
                tg,
                old_chat_id,
                old_message_id,
                f"↗️ Заявка #{video['id']} перенесена в актуальную карточку ниже.",
                actor,
            )
        return {
            "pending_count": total,
            "global_pending_count": global_total,
            "active_video_id": int(video["id"]),
            "active_message_id": message_id,
            "sent": True,
            "queue_filter_type": _queue_filter(state)[0],
            "queue_filter_value": _queue_filter(state)[1],
        }


def _queue_stale_text(current_video_id: int | None) -> str:
    if current_video_id:
        return f"Эта карточка устарела. Текущая заявка #{current_video_id}."
    return ADMIN_QUEUE_STALE_MESSAGE


def _answer_queue_callback(
    tg: TelegramClient,
    callback_id: str,
    text: str | None = None,
    *,
    show_alert: bool = False,
) -> None:
    try:
        tg.answer_callback_query(callback_id, text, show_alert=show_alert)
    except Exception:
        pass


def handle_stale_admin_callback(tg: TelegramClient, actor: Actor, callback_id: str) -> None:
    if not is_admin(actor.tg_id):
        _answer_queue_callback(tg, callback_id, "Это действие доступно только админам.", show_alert=True)
        return
    try:
        state = db.fetch_one(
            "SELECT active_video_id FROM admin_queue_state WHERE queue_name = %s",
            (ADMIN_QUEUE_NAME,),
        )
        current_id = int(state["active_video_id"]) if state and state.get("active_video_id") else None
    except Exception:
        current_id = None
    _answer_queue_callback(tg, callback_id, _queue_stale_text(current_id), show_alert=True)


def _lock_current_queue_item(
    conn,
    video_id: int,
    chat_id: int,
    message_id: int | None,
) -> tuple[dict[str, Any], dict[str, Any] | None, str | None]:
    state = _queue_state_for_update(conn)
    current_id = int(state["active_video_id"]) if state.get("active_video_id") else None
    if (
        current_id != video_id
        or int(state.get("active_chat_id") or 0) != int(chat_id)
        or int(state.get("active_message_id") or 0) != int(message_id or 0)
    ):
        return state, None, _queue_stale_text(current_id)
    with conn.cursor() as cur:
        cur.execute("SELECT id, status, batch_id FROM videos WHERE id = %s FOR UPDATE", (video_id,))
        locked = cur.fetchone()
    if not locked or locked["status"] != "pending":
        return state, None, f"Заявка #{video_id} уже обработана другим админом."
    return state, locked, None


def _active_queue_card(
    conn,
    video_id: int,
    state: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], int, int]:
    queue_state = state or _queue_state_for_update(conn)
    video = get_video_by_id(conn, video_id)
    total = _pending_video_count(conn, queue_state)
    return video, total, _queue_position(conn, video, queue_state)


def _refresh_active_queue_card(
    tg: TelegramClient,
    actor: Actor,
    video_id: int,
    message_id: int | None,
) -> str | None:
    with db.transaction() as conn:
        state, _, error = _lock_current_queue_item(conn, video_id, actor.chat_id, message_id)
        if error:
            return error
        video, total, position = _active_queue_card(conn, video_id, state)
        _edit_message_text_idempotent(
            tg,
            actor.chat_id,
            int(message_id),
            format_admin_queue_card(video, total, position, _queue_filter_label(state)),
            admin_queue_keyboard(video_id),
        )
    return None


def _show_admin_queue_date_options(
    tg: TelegramClient,
    actor: Actor,
    video_id: int,
    message_id: int | None,
) -> str | None:
    with db.transaction() as conn:
        _, _, error = _lock_current_queue_item(conn, video_id, actor.chat_id, message_id)
        if error:
            return error
        keyboard = inline_keyboard(
            [
                [("Сегодня", f"admq:setdate:{video_id}:today"), ("Вчера", f"admq:setdate:{video_id}:yesterday")],
                [("Позавчера", f"admq:setdate:{video_id}:before_yesterday")],
                [("Ввести вручную", f"admq:manualdate:{video_id}")],
                [("Назад", f"admq:refresh:{video_id}")],
            ]
        )
        _edit_message_text_idempotent(
            tg,
            actor.chat_id,
            int(message_id),
            f"Заявка #{video_id}\nУкажите дату публикации:\n{ADMIN_DATE_PROMPT}",
            keyboard,
        )
    return None


def admin_project_keyboard(video_id: int) -> dict[str, Any]:
    buttons = [
        (f"{project['emoji']} {project['name']}", f"admq:setproject:{video_id}:{project['code']}")
        for project in PROJECTS
        if project["code"] != "other"
    ]
    rows = [buttons[index : index + 2] for index in range(0, len(buttons), 2)]
    rows.append([("➕ Другой проект", f"admq:projectother:{video_id}")])
    rows.append([("Назад", f"admq:refresh:{video_id}")])
    return inline_keyboard(rows)


def _show_admin_queue_project_options(
    tg: TelegramClient,
    actor: Actor,
    video_id: int,
    message_id: int | None,
) -> str | None:
    with db.transaction() as conn:
        _, _, error = _lock_current_queue_item(conn, video_id, actor.chat_id, message_id)
        if error:
            return error
        _edit_message_text_idempotent(
            tg,
            actor.chat_id,
            int(message_id),
            f"Заявка #{video_id}\n{PROJECT_PROMPT}",
            admin_project_keyboard(video_id),
        )
    return None


def _set_active_queue_project(
    tg: TelegramClient,
    actor: Actor,
    video_id: int,
    active_message_id: int,
    *,
    project_id: int | None,
    project_code: str,
    project_name: str,
) -> str | None:
    moved_out_of_filter = False
    updated_video: dict[str, Any] | None = None
    with db.transaction() as conn:
        state, locked, error = _lock_current_queue_item(
            conn,
            video_id,
            actor.chat_id,
            active_message_id,
        )
        if error:
            return error
        before = get_video_by_id(conn, video_id)
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE videos
                SET project_id = %s,
                    project_code = %s,
                    project_name = %s,
                    updated_at = now()
                WHERE id = %s AND status = 'pending'
                """,
                (project_id, project_code, project_name, video_id),
            )
        if locked and locked.get("batch_id"):
            recalculate_batch(conn, int(locked["batch_id"]))
        db.log_event(
            conn,
            entity_type="video",
            entity_id=video_id,
            action="project_changed",
            actor_tg_id=actor.tg_id,
            actor_username=actor.username,
            before_data={"project_code": before.get("project_code"), "project_name": before.get("project_name")},
            after_data={"project_code": project_code, "project_name": project_name},
        )
        updated_video = get_video_by_id(conn, video_id)
        moved_out_of_filter = not _video_matches_queue_filter(updated_video, state)
        if moved_out_of_filter:
            _clear_queue_state(conn)
        else:
            video, total, position = _active_queue_card(conn, video_id, state)
            _edit_message_text_idempotent(
                tg,
                actor.chat_id,
                active_message_id,
                format_admin_queue_card(video, total, position, _queue_filter_label(state)),
                admin_queue_keyboard(video_id),
            )
    if moved_out_of_filter and updated_video:
        _archive_queue_message(
            tg,
            actor.chat_id,
            active_message_id,
            f"📂 Проект заявки #{video_id} изменён; она больше не входит в выбранный фильтр.",
            actor,
        )
        pump_admin_queue(tg, actor)
    _safe_refresh_admin_dashboard(tg, actor)
    return None


def _start_admin_queue_project_other(
    tg: TelegramClient,
    actor: Actor,
    video_id: int,
    message_id: int | None,
) -> str | None:
    with db.transaction() as conn:
        _, _, error = _lock_current_queue_item(conn, video_id, actor.chat_id, message_id)
        if error:
            return error
    db.set_session(
        tg_id=actor.tg_id,
        chat_id=actor.chat_id,
        username=actor.username,
        state="admin:project_other",
        data={"video_id": video_id, "active_message_id": int(message_id or 0)},
    )
    tg.send_message(actor.chat_id, PROJECT_OTHER_PROMPT)
    return None


def handle_admin_project_other_message(
    tg: TelegramClient,
    actor: Actor,
    data: dict[str, Any],
    text: str,
) -> None:
    if not require_admin(tg, actor):
        db.clear_session(actor.tg_id)
        return
    project_name = normalize_custom_project_name(text)
    if not project_name:
        tg.send_message(actor.chat_id, PROJECT_OTHER_INVALID_MESSAGE)
        return
    error = _set_active_queue_project(
        tg,
        actor,
        int(data.get("video_id") or 0),
        int(data.get("active_message_id") or 0),
        project_id=None,
        project_code="other",
        project_name=project_name,
    )
    if error:
        tg.send_message(actor.chat_id, error)
        return
    db.clear_session(actor.tg_id)


def _start_admin_queue_manual_date(
    tg: TelegramClient,
    actor: Actor,
    video_id: int,
    message_id: int | None,
) -> str | None:
    with db.transaction() as conn:
        state, _, error = _lock_current_queue_item(conn, video_id, actor.chat_id, message_id)
        if error:
            return error
        claimed_at = state.get("claimed_at")
        claim_fresh = bool(
            state.get("claimed_by_tg_id")
            and claimed_at
            and claimed_at > datetime.now(timezone.utc) - timedelta(seconds=ADMIN_DATE_CLAIM_SECONDS)
        )
        if claim_fresh and int(state["claimed_by_tg_id"]) != actor.tg_id:
            claimant = (
                f"@{state['claimed_by_username']}"
                if state.get("claimed_by_username")
                else str(state["claimed_by_tg_id"])
            )
            return f"Заявку #{video_id} сейчас оформляет {claimant}."
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE admin_queue_state
                SET claimed_by_tg_id = %s,
                    claimed_by_username = %s,
                    claimed_at = now(),
                    updated_at = now()
                WHERE queue_name = %s
                """,
                (actor.tg_id, actor.username, ADMIN_QUEUE_NAME),
            )
    db.set_session(
        tg_id=actor.tg_id,
        chat_id=actor.chat_id,
        username=actor.username,
        state="admin:date",
        data={
            "video_id": video_id,
            "queue_name": ADMIN_QUEUE_NAME,
            "active_message_chat_id": actor.chat_id,
            "active_message_id": int(message_id),
        },
    )
    tg.send_message(
        actor.chat_id,
        f"Заявка #{video_id} — введите дату публикации:\n{ADMIN_DATE_PROMPT}",
    )
    return None


def _set_active_queue_publish_date(
    tg: TelegramClient,
    actor: Actor,
    video_id: int,
    publish_date: date,
    active_chat_id: int,
    active_message_id: int,
) -> str | None:
    with db.transaction() as conn:
        state, locked, error = _lock_current_queue_item(conn, video_id, active_chat_id, active_message_id)
        if error:
            return error
        claimed_at = state.get("claimed_at")
        claim_fresh = bool(
            state.get("claimed_by_tg_id")
            and claimed_at
            and claimed_at > datetime.now(timezone.utc) - timedelta(seconds=ADMIN_DATE_CLAIM_SECONDS)
        )
        if claim_fresh and int(state["claimed_by_tg_id"]) != actor.tg_id:
            claimant = (
                f"@{state['claimed_by_username']}"
                if state.get("claimed_by_username")
                else str(state["claimed_by_tg_id"])
            )
            return f"Заявку #{video_id} сейчас оформляет {claimant}."
        before = get_video_by_id(conn, video_id)
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE videos
                SET publish_date = %s,
                    publish_date_set_by_tg_id = %s,
                    publish_date_set_by_username = %s,
                    publish_date_set_at = now(),
                    updated_at = now()
                WHERE id = %s AND status = 'pending'
                """,
                (publish_date, actor.tg_id, actor.username, video_id),
            )
            cur.execute(
                """
                UPDATE admin_queue_state
                SET claimed_by_tg_id = NULL,
                    claimed_by_username = NULL,
                    claimed_at = NULL,
                    updated_at = now()
                WHERE queue_name = %s
                """,
                (ADMIN_QUEUE_NAME,),
            )
        if locked and locked.get("batch_id"):
            recalculate_batch(conn, int(locked["batch_id"]))
        db.log_event(
            conn,
            entity_type="video",
            entity_id=video_id,
            action="publish_date_set",
            actor_tg_id=actor.tg_id,
            actor_username=actor.username,
            before_data={"publish_date": _date_iso(before.get("publish_date"))},
            after_data={"publish_date": publish_date.isoformat()},
        )
        video, total, position = _active_queue_card(conn, video_id, state)
        _edit_message_text_idempotent(
            tg,
            active_chat_id,
            active_message_id,
            format_admin_queue_card(video, total, position, _queue_filter_label(state)),
            admin_queue_keyboard(video_id),
        )
    return None


def _format_processed_queue_card(
    video: dict[str, Any],
    status: str,
    actor: Actor,
    *,
    sheet_ok: bool = True,
) -> str:
    labels = {
        "approved": f"✅ Заявка #{video['id']} одобрена",
        "needs_revision": f"🛠 Заявка #{video['id']} возвращена на правку",
        "duplicate": f"♻️ Заявка #{video['id']} отмечена как дубль",
        "deleted": f"🗑 Заявка #{video['id']} удалена",
    }
    lines = [labels[status], f"Проект: {video.get('project_name') or 'не указан'}"]
    if status == "approved":
        lines.append(f"Дата публикации: {_format_ddmmyyyy(video.get('publish_date'))}")
        if not sheet_ok:
            lines.append("Google Sheets: ошибка синхронизации, используйте /sync_sheets")
    lines.append(f"Проверил: {user_label(actor.username, actor.tg_id)}")
    return "\n".join(lines)


def _notify_submitter_of_queue_result(
    tg: TelegramClient,
    video: dict[str, Any],
    status: str,
) -> None:
    chat_id = video.get("added_by_tg_id")
    if not chat_id:
        return
    messages = {
        "approved": f"✅ Заявка #{video['id']} одобрена.",
        "needs_revision": f"🛠 Заявка #{video['id']} возвращена на правку.",
        "duplicate": f"♻️ Заявка #{video['id']} отмечена как дубль.",
        "deleted": f"🗑 Заявка #{video['id']} удалена.",
    }
    try:
        tg.send_message(int(chat_id), messages[status])
    except Exception:
        pass


def _process_admin_queue_action(
    tg: TelegramClient,
    actor: Actor,
    video_id: int,
    message_id: int | None,
    status: str,
) -> str | None:
    with db.transaction() as conn:
        _, locked, error = _lock_current_queue_item(conn, video_id, actor.chat_id, message_id)
        if error:
            return error
        before = get_video_by_id(conn, video_id)
        if status == "approved":
            approval_error = admin_approval_error(before)
            if approval_error:
                return approval_error
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE videos
                SET status = %s,
                    sheet_sync_status = CASE WHEN %s = 'approved' THEN 'queued' ELSE sheet_sync_status END,
                    sheet_sync_error = CASE WHEN %s = 'approved' THEN NULL ELSE sheet_sync_error END,
                    checked_by_tg_id = %s,
                    checked_by_username = %s,
                    checked_at = now(),
                    updated_at = now()
                WHERE id = %s AND status = 'pending'
                """,
                (status, status, status, actor.tg_id, actor.username, video_id),
            )
            cur.execute("DELETE FROM admin_locks WHERE video_id = %s", (video_id,))
        _clear_queue_state(conn)
        if locked and locked.get("batch_id"):
            recalculate_batch(conn, int(locked["batch_id"]))
        db.log_event(
            conn,
            entity_type="video",
            entity_id=video_id,
            action=status,
            actor_tg_id=actor.tg_id,
            actor_username=actor.username,
            before_data={"status": before.get("status")},
            after_data={"status": status},
        )
        video = get_video_by_id(conn, video_id)

    sheet_ok = sync_video_after_approval(video, actor) if status == "approved" else True
    try:
        tg.edit_message_text(
            actor.chat_id,
            int(message_id),
            _format_processed_queue_card(video, status, actor, sheet_ok=sheet_ok),
            {"inline_keyboard": []},
        )
    except Exception as exc:
        record_system_log(
            "admin_queue_finalize_failed",
            "video",
            video_id,
            telegram_failure_payload(exc, actor.chat_id, "finalize_queue_card"),
            actor,
        )
    chat_id = video.get("added_by_tg_id")
    if chat_id:
        messages = {
            "approved": f"✅ Заявка #{video['id']} одобрена.",
            "needs_revision": f"🛠 Заявка #{video['id']} возвращена на правку.",
            "duplicate": f"♻️ Заявка #{video['id']} отмечена как дубль.",
            "deleted": f"🗑 Заявка #{video['id']} удалена.",
        }
        try:
            jobs.enqueue_telegram_notification(
                int(chat_id),
                messages[status],
                event_key=f"queue-result:{status}:{video_id}:{chat_id}",
            )
        except Exception as exc:
            record_system_log(
                "queue_result_notification_queue_failed",
                "video",
                video_id,
                {"error": _safe_error(exc)},
                actor,
            )
    _safe_refresh_admin_dashboard(tg, actor)
    try:
        result = pump_admin_queue(tg, actor)
        record_system_log(
            "admin_queue_pumped",
            "admin_queue",
            result.get("active_video_id"),
            {"source": f"after_{status}", **result},
            actor,
        )
    except Exception as exc:
        record_system_log(
            "admin_queue_pump_failed",
            "video",
            video_id,
            telegram_failure_payload(exc, get_settings().admin_chat_id, "pump_after_action"),
            actor,
        )
        try:
            jobs.enqueue_admin_queue_pump()
        except Exception:
            pass
    _safe_refresh_admin_dashboard(tg, actor)
    return None


def admin_approval_error(video: dict[str, Any]) -> str | None:
    if not video.get("project_code") or not video.get("project_name"):
        return "Сначала укажи проект."
    if not video.get("publish_date"):
        return "Сначала укажи дату публикации."
    return None


def handle_admin_queue_callback(
    tg: TelegramClient,
    actor: Actor,
    data: str,
    message_id: int | None,
    callback_id: str,
) -> None:
    if not is_admin(actor.tg_id):
        _answer_queue_callback(tg, callback_id, "Это действие доступно только админам.", show_alert=True)
        return
    try:
        parts = data.split(":")
        action = parts[1]
        video_id = int(parts[2])
        error: str | None
        if action == "date":
            error = _show_admin_queue_date_options(tg, actor, video_id, message_id)
        elif action == "project":
            error = _show_admin_queue_project_options(tg, actor, video_id, message_id)
        elif action == "projectother":
            error = _start_admin_queue_project_other(tg, actor, video_id, message_id)
        elif action == "setproject" and len(parts) == 4:
            project = get_active_project(parts[3])
            if not project or project["code"] == "other":
                error = "Проект недоступен."
            else:
                error = _set_active_queue_project(
                    tg,
                    actor,
                    video_id,
                    int(message_id or 0),
                    project_id=int(project["id"]),
                    project_code=str(project["code"]),
                    project_name=str(project["name"]),
                )
        elif action == "manualdate":
            error = _start_admin_queue_manual_date(tg, actor, video_id, message_id)
        elif action == "setdate" and len(parts) == 4:
            preset = {
                "today": "Сегодня",
                "yesterday": "Вчера",
                "before_yesterday": "Позавчера",
            }.get(parts[3])
            if not preset:
                error = "Неизвестный вариант даты."
            else:
                publish_date = parse_publish_date(preset)
                error = _set_active_queue_publish_date(
                    tg,
                    actor,
                    video_id,
                    publish_date,
                    actor.chat_id,
                    int(message_id or 0),
                )
        elif action == "refresh":
            error = _refresh_active_queue_card(tg, actor, video_id, message_id)
        elif action in {"approve", "revision", "duplicate", "delete"}:
            status = {
                "approve": "approved",
                "revision": "needs_revision",
                "duplicate": "duplicate",
                "delete": "deleted",
            }[action]
            error = _process_admin_queue_action(tg, actor, video_id, message_id, status)
        else:
            error = ADMIN_QUEUE_STALE_MESSAGE
    except (IndexError, TypeError, ValueError):
        error = ADMIN_QUEUE_STALE_MESSAGE
    except Exception as exc:
        record_system_log(
            "admin_queue_callback_failed",
            "telegram_callback",
            None,
            {"error": _safe_error(exc), "data": data[:100]},
            actor,
        )
        error = "Не удалось обработать очередь. Попробуйте /admin."
    _answer_queue_callback(tg, callback_id, error, show_alert=bool(error))


def reset_admin_queue_command(tg: TelegramClient, actor: Actor) -> None:
    if not require_superadmin(tg, actor):
        return
    old_cards = db.fetch_all(
        """
        SELECT id, admin_message_chat_id, admin_message_id
        FROM videos
        WHERE status = 'pending'
          AND admin_message_chat_id IS NOT NULL
          AND admin_message_id IS NOT NULL
        ORDER BY created_at ASC, id ASC
        """
    )
    with db.transaction() as conn:
        _queue_state_for_update(conn)
        _clear_queue_state(conn)
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM user_sessions
                WHERE state IN ('admin:date', 'admin:project_other', 'admin:search', 'admin:person')
                """
            )
            cur.execute(
                """
                UPDATE videos
                SET admin_message_chat_id = NULL,
                    admin_message_id = NULL,
                    admin_notified_at = NULL,
                    updated_at = now()
                WHERE status = 'pending'
                """
            )
        db.log_event(
            conn,
            entity_type="admin_queue",
            entity_id=None,
            action="reset",
            actor_tg_id=actor.tg_id,
            actor_username=actor.username,
            after_data={
                "old_card_count": len(old_cards),
                "archive_attempt_limit": ADMIN_RESET_ARCHIVE_LIMIT,
            },
        )
    result = pump_admin_queue(tg, actor)
    record_system_log(
        "admin_queue_pumped",
        "admin_queue",
        result.get("active_video_id"),
        {"source": "reset_admin_queue", **result},
        actor,
    )
    _safe_refresh_admin_dashboard(tg, actor)
    for row in old_cards[:ADMIN_RESET_ARCHIVE_LIMIT]:
        _archive_queue_message(
            tg,
            int(row["admin_message_chat_id"]),
            int(row["admin_message_id"]),
            f"Архивная карточка заявки #{row['id']}. Используйте текущую очередь: /admin",
            actor,
        )
    if result["pending_count"] == 0:
        tg.send_message(actor.chat_id, "Очередь пуста. Pending-заявок: 0.")


def return_missing_dates_command(tg: TelegramClient, actor: Actor) -> None:
    if not require_admin(tg, actor):
        return
    if not jobs.background_jobs_enabled():
        tg.send_message(actor.chat_id, "Фоновые задания временно отключены.")
        return
    row = db.fetch_one(
        "SELECT count(*) AS count FROM videos WHERE status = 'pending' AND publish_date IS NULL"
    ) or {}
    count = int(row.get("count") or 0)
    tg.send_message(
        actor.chat_id,
        f"Найдено заявок без даты: {count}\n\nВернуть их авторам на заполнение даты?",
        inline_keyboard(
            [
                [("Да, вернуть", "missingdate:return")],
                [("Отмена", "missingdate:cancel")],
            ]
        ),
    )


def jobs_status_command(tg: TelegramClient, actor: Actor) -> None:
    if not require_admin(tg, actor):
        return
    tg.send_message(actor.chat_id, jobs.format_jobs_status(jobs.jobs_status_snapshot()))


def retry_failed_jobs_command(tg: TelegramClient, actor: Actor) -> None:
    if not is_superadmin(actor.tg_id):
        tg.send_message(actor.chat_id, "Команда доступна только суперадмину.")
        return
    tg.send_message(
        actor.chat_id,
        "Повторить временно упавшие фоновые задания? Dead и permanent jobs не изменятся.",
        inline_keyboard(
            [
                [("Повторить", "jobretry:confirm")],
                [("Отмена", "jobretry:cancel")],
            ]
        ),
    )


def handle_retry_failed_jobs_callback(
    tg: TelegramClient,
    actor: Actor,
    data: str,
    callback_id: str,
) -> None:
    if not is_superadmin(actor.tg_id):
        _answer_queue_callback(tg, callback_id, "Только для суперадмина.", show_alert=True)
        return
    if data == "jobretry:cancel":
        _answer_queue_callback(tg, callback_id, "Отменено.")
        return
    if data != "jobretry:confirm":
        _answer_queue_callback(tg, callback_id, "Действие устарело.", show_alert=True)
        return
    count = jobs.retry_failed_jobs()
    _answer_queue_callback(tg, callback_id, f"Поставлено в очередь: {count}.", show_alert=True)
    tg.send_message(actor.chat_id, jobs.format_jobs_status(jobs.jobs_status_snapshot()))


def handle_missing_date_callback(
    tg: TelegramClient,
    actor: Actor,
    data: str,
    callback_id: str,
) -> None:
    if not is_admin(actor.tg_id):
        _answer_queue_callback(tg, callback_id, "Это действие доступно только админам.", show_alert=True)
        return
    if data == "missingdate:cancel":
        _answer_queue_callback(tg, callback_id)
        tg.send_message(actor.chat_id, "Возврат заявок без даты отменён.")
        return
    if data != "missingdate:return":
        _answer_queue_callback(tg, callback_id, "Действие устарело.", show_alert=True)
        return
    _answer_queue_callback(tg, callback_id)
    bulk_return_missing_dates(tg, actor)


def _missing_date_notification_text(video: dict[str, Any]) -> str:
    link_label = "Instagram"
    link = video.get("instagram_url")
    if not link:
        link_label = "YouTube"
        link = video.get("youtube_url")
    return (
        f"Заявка #{video['id']} возвращена на заполнение даты.\n\n"
        f"Проект: {video.get('project_name') or 'не указан'}\n"
        f"{link_label}: {link or 'не указан'}\n\n"
        "Нажми кнопку ниже и укажи дату публикации."
    )


def bulk_return_missing_dates(tg: TelegramClient, actor: Actor) -> dict[str, Any]:
    if not jobs.background_jobs_enabled():
        tg.send_message(actor.chat_id, "Фоновые задания временно отключены.")
        return {"operation_id": None, "returned_count": 0, "total_count": 0}
    with db.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT *
                FROM bulk_operations
                WHERE kind = 'return_missing_dates'
                  AND status IN ('queued', 'processing')
                ORDER BY created_at DESC
                LIMIT 1
                FOR UPDATE
                """
            )
            operation = cur.fetchone()
            if not operation:
                cur.execute(
                    "SELECT count(*) AS count FROM videos WHERE status = 'pending' AND publish_date IS NULL"
                )
                total_count = int(cur.fetchone()["count"])
                cur.execute(
                    """
                    INSERT INTO bulk_operations (
                        kind, status, total_count, created_by_tg_id, created_by_username
                    )
                    VALUES ('return_missing_dates', 'queued', %s, %s, %s)
                    RETURNING *
                    """,
                    (total_count, actor.tg_id, actor.username),
                )
                operation = cur.fetchone()
                jobs.enqueue_job(
                    "bulk_return_missing_dates",
                    {"operation_id": int(operation["id"])},
                    dedupe_key=f"bulk:{operation['id']}:chunk:0",
                    priority=30,
                    conn=conn,
                )
            else:
                total_count = int(operation["total_count"])
            db.log_event(
                conn,
                entity_type="bulk_operation",
                entity_id=int(operation["id"]),
                action="bulk_operation_started",
                actor_tg_id=actor.tg_id,
                actor_username=actor.username,
                after_data={"total_count": total_count},
            )
    operation_id = int(operation["id"])
    tg.send_message(
        actor.chat_id,
        "Возврат заявок запущен.\n\n"
        f"Операция #{operation_id}\n"
        f"Найдено заявок: {total_count}\n"
        "Прогресс: /jobs_status",
    )
    return {
        "operation_id": operation_id,
        "returned_count": 0,
        "notified_count": 0,
        "failed_count": 0,
        "total_count": total_count,
    }


def _missing_date_revision_error(video: dict[str, Any] | None, actor: Actor) -> str | None:
    if not video:
        return "Заявка не найдена."
    if video.get("added_by_tg_id") != actor.tg_id and not is_admin(actor.tg_id):
        return "Можно указывать дату только в своей заявке."
    if video.get("status") != "needs_revision":
        return "Эта заявка сейчас не ожидает правку."
    if video.get("publish_date"):
        return "В этой заявке дата уже указана."
    return None


def _missing_date_revision_keyboard(video_id: int) -> dict[str, Any]:
    return inline_keyboard(
        [
            [
                ("Сегодня", f"revdate:set:{video_id}:today"),
                ("Вчера", f"revdate:set:{video_id}:yesterday"),
            ],
            [("Ввести вручную", f"revdate:manual:{video_id}")],
        ]
    )


def start_missing_date_revision(tg: TelegramClient, actor: Actor, video_id: int) -> None:
    video = get_video_by_id_outside(video_id)
    error = _missing_date_revision_error(video, actor)
    if error:
        tg.send_message(actor.chat_id, error)
        return
    tg.send_message(
        actor.chat_id,
        f"Заявка #{video_id} — укажи дату публикации",
        _missing_date_revision_keyboard(video_id),
    )


def start_missing_date_revision_manual(tg: TelegramClient, actor: Actor, video_id: int) -> None:
    video = get_video_by_id_outside(video_id)
    error = _missing_date_revision_error(video, actor)
    if error:
        tg.send_message(actor.chat_id, error)
        return
    db.set_session(
        tg_id=actor.tg_id,
        chat_id=actor.chat_id,
        username=actor.username,
        state="revision:missing_date",
        data={"video_id": video_id, "flow": "revision_missing_date"},
    )
    tg.send_message(actor.chat_id, NEW_DATE_MANUAL_PROMPT)


def handle_missing_date_revision_callback(
    tg: TelegramClient,
    actor: Actor,
    data: str,
) -> None:
    parts = data.split(":")
    try:
        if len(parts) == 2:
            start_missing_date_revision(tg, actor, int(parts[1]))
            return
        if len(parts) == 3 and parts[1] == "manual":
            start_missing_date_revision_manual(tg, actor, int(parts[2]))
            return
        if len(parts) == 4 and parts[1] == "set":
            preset = NEW_DATE_PRESETS.get(parts[3], "")
            publish_date = parse_new_submission_date(preset)
            restore_missing_date(tg, actor, int(parts[2]), publish_date)
            return
    except (TypeError, ValueError):
        pass
    tg.send_message(actor.chat_id, "Действие устарело. Открой /my_requests.")


def handle_missing_date_revision_message(
    tg: TelegramClient,
    actor: Actor,
    data: dict[str, Any],
    text: str,
) -> None:
    try:
        publish_date = parse_new_submission_date(text)
    except ValueError:
        tg.send_message(actor.chat_id, NEW_DATE_INVALID_MESSAGE)
        return
    restore_missing_date(tg, actor, int(data.get("video_id") or 0), publish_date)


def restore_missing_date(
    tg: TelegramClient,
    actor: Actor,
    video_id: int,
    publish_date: date,
) -> dict[str, Any] | None:
    actor_is_admin = is_admin(actor.tg_id)
    old_active_card: tuple[int, int, int] | None = None
    with db.transaction() as conn:
        state = _queue_state_for_update(conn)
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, status, publish_date, added_by_tg_id, batch_id
                FROM videos
                WHERE id = %s
                FOR UPDATE
                """,
                (video_id,),
            )
            locked = cur.fetchone()
            if not locked:
                tg.send_message(actor.chat_id, "Заявка не найдена.")
                return None
            if locked.get("added_by_tg_id") != actor.tg_id and not actor_is_admin:
                tg.send_message(actor.chat_id, "Можно указывать дату только в своей заявке.")
                return None
            if locked.get("status") != "needs_revision" or locked.get("publish_date"):
                tg.send_message(actor.chat_id, "Эта заявка уже не ожидает заполнение даты.")
                return None
            cur.execute(
                """
                UPDATE videos
                SET publish_date = %s,
                    status = 'pending',
                    checked_by_tg_id = NULL,
                    checked_by_username = NULL,
                    checked_at = NULL,
                    publish_date_set_by_tg_id = %s,
                    publish_date_set_by_username = %s,
                    publish_date_set_at = now(),
                    updated_at = now()
                WHERE id = %s
                """,
                (publish_date, actor.tg_id, actor.username, video_id),
            )
        if locked.get("batch_id"):
            recalculate_batch(conn, int(locked["batch_id"]))
        db.log_event(
            conn,
            entity_type="video",
            entity_id=video_id,
            action="missing_date_revision_submitted",
            actor_tg_id=actor.tg_id,
            actor_username=actor.username,
            before_data={"status": "needs_revision", "publish_date": None},
            after_data={"status": "pending", "publish_date": publish_date.isoformat()},
        )
        active_id = int(state["active_video_id"]) if state.get("active_video_id") else None
        oldest = _oldest_pending_video(conn, state)
        if active_id and oldest and active_id != int(oldest["id"]):
            if state.get("active_chat_id") and state.get("active_message_id"):
                old_active_card = (
                    int(state["active_chat_id"]),
                    int(state["active_message_id"]),
                    active_id,
                )
            _clear_queue_state(conn)
        video = get_video_by_id(conn, video_id)

    db.clear_session(actor.tg_id)
    if old_active_card:
        _archive_queue_message(
            tg,
            old_active_card[0],
            old_active_card[1],
            f"FIFO обновлена после возврата заявки #{video_id} в очередь.",
            actor,
        )
    tg.send_message(
        actor.chat_id,
        f"✅ Дата добавлена. Заявка #{video_id} снова отправлена на проверку.\n"
        f"Дата: {_format_ddmmyyyy(publish_date)}",
    )
    _safe_refresh_admin_dashboard(tg, actor)
    try:
        pump_admin_queue(tg, actor)
    except Exception as exc:
        record_system_log(
            "admin_queue_pump_failed",
            "video",
            video_id,
            telegram_failure_payload(exc, get_settings().admin_chat_id, "pump_after_date_revision"),
            actor,
        )
    _safe_refresh_admin_dashboard(tg, actor)
    return video


def show_my_requests(tg: TelegramClient, actor: Actor) -> None:
    rows = db.fetch_all(
        VIDEO_SELECT
        + """
        WHERE v.added_by_tg_id = %s
        ORDER BY v.created_at DESC
        LIMIT 10
        """,
        (actor.tg_id,),
    )
    if not rows:
        tg.send_message(actor.chat_id, "У вас пока нет заявок.")
        return
    tg.send_message(actor.chat_id, f"Ваши последние заявки: {len(rows)}")
    for row in rows:
        buttons = [[("Дополнить ссылки", f"links:{row['id']}")]]
        if row.get("status") == "needs_revision":
            if not row.get("publish_date"):
                buttons.insert(0, [("Указать дату", f"revdate:{row['id']}")])
            else:
                buttons.insert(0, [("Исправить", f"revise:{row['id']}")])
        tg.send_message(
            actor.chat_id,
            format_video_card(row, title="Моя заявка"),
            inline_keyboard(buttons),
        )


def start_revision(tg: TelegramClient, actor: Actor, video_id: int) -> None:
    video = get_video_by_id_outside(video_id)
    if not video:
        tg.send_message(actor.chat_id, "Заявка не найдена.")
        return
    if video.get("added_by_tg_id") != actor.tg_id and not is_admin(actor.tg_id):
        tg.send_message(actor.chat_id, "Можно исправлять только свои заявки.")
        return
    if video.get("status") != "needs_revision":
        tg.send_message(actor.chat_id, "Эта заявка сейчас не ожидает правку.")
        return
    if not video.get("publish_date"):
        start_missing_date_revision(tg, actor, video_id)
        return
    video_type = normalize_video_type(video.get("video_type"))
    data = {
        "edit_video_id": video_id,
        "video_type": video_type,
        "platform_flow": PLATFORM_FLOW_BIGRECAP if video_type == VIDEO_TYPE_BIGRECAP else PLATFORM_FLOW_REGULAR,
        "project_id": video.get("project_id"),
        "project_code": video.get("project_code"),
        "project_name": video.get("project_name"),
        "publish_date": _date_iso(video.get("publish_date")),
        "instagram_url": None if video_type == VIDEO_TYPE_BIGRECAP else video.get("instagram_url"),
        "instagram_id": None if video_type == VIDEO_TYPE_BIGRECAP else video.get("instagram_id"),
        "youtube_url": video.get("youtube_url") if video_type == VIDEO_TYPE_BIGRECAP else None,
        "youtube_id": video.get("youtube_id") if video_type == VIDEO_TYPE_BIGRECAP else None,
        "tiktok_url": None if video_type == VIDEO_TYPE_BIGRECAP else video.get("tiktok_url"),
        "tiktok_id": None if video_type == VIDEO_TYPE_BIGRECAP else video.get("tiktok_id"),
    }
    tg.send_message(actor.chat_id, "Ок, исправим заявку и вернём её в очередь.")
    if data.get("project_code") and data.get("project_name"):
        db.set_session(
            tg_id=actor.tg_id,
            chat_id=actor.chat_id,
            username=actor.username,
            state="new:author",
            data=data,
        )
        ask_people(tg, actor, "author")
    else:
        ask_submission_project(tg, actor, data)


def show_admin(tg: TelegramClient, actor: Actor) -> None:
    if not require_admin(tg, actor):
        return
    _safe_refresh_admin_dashboard(tg, actor, immediate=True)
    result = pump_admin_queue(tg, actor, force_repost=True)
    record_system_log(
        "admin_queue_pumped",
        "admin_queue",
        result.get("active_video_id"),
        {"source": "admin", **result},
        actor,
    )
    _safe_refresh_admin_dashboard(tg, actor, immediate=True)
    if result["pending_count"] == 0:
        tg.send_message(actor.chat_id, "Очередь пуста. Pending-заявок: 0.")


def assign_orphan_pending(conn, actor: Actor) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS count FROM videos WHERE status = 'pending' AND batch_id IS NULL")
        count = int(cur.fetchone()["count"])
        if count == 0:
            return
        cur.execute(
            """
            INSERT INTO batches (created_by_tg_id, created_by_username)
            VALUES (%s, %s)
            RETURNING id
            """,
            (actor.tg_id, actor.username),
        )
        batch_id = int(cur.fetchone()["id"])
        cur.execute("UPDATE videos SET batch_id = %s WHERE status = 'pending' AND batch_id IS NULL", (batch_id,))
        recalculate_batch(conn, batch_id)


def send_batch_summary(
    tg: TelegramClient,
    chat_id: int,
    batch_id: int,
    edit_message_id: int | None = None,
) -> None:
    batch = db.fetch_one("SELECT * FROM batches WHERE id = %s", (batch_id,))
    if not batch:
        tg.send_message(chat_id, "Пачка не найдена.")
        return
    text = format_batch_summary(batch)
    keyboard = inline_keyboard(
        [
            [("Открыть очередь", f"adm:open:{batch_id}:0")],
            [("Одобрить чистые", f"adm:clean:{batch_id}")],
            [("Показать дубли", f"adm:sum:{batch_id}"), ("Показать проблемные", f"adm:sum:{batch_id}")],
            [("Отложить", "cmd:admin")],
        ]
    )
    if edit_message_id:
        try:
            tg.edit_message_text(chat_id, edit_message_id, text, keyboard)
            return
        except Exception:
            pass
    tg.send_message(chat_id, text, keyboard)


def show_queue_item(
    tg: TelegramClient,
    actor: Actor,
    batch_id: int,
    index: int,
    edit_message_id: int | None = None,
) -> None:
    if not require_admin(tg, actor):
        return
    pending_count = db.fetch_one(
        "SELECT count(*) AS count FROM videos WHERE batch_id = %s AND status = 'pending'",
        (batch_id,),
    )
    total = int((pending_count or {}).get("count") or 0)
    if total == 0:
        with db.transaction() as conn:
            recalculate_batch(conn, batch_id)
        send_batch_summary(tg, actor.chat_id, batch_id, edit_message_id)
        return
    safe_index = max(0, min(index, total - 1))
    row = db.fetch_one(
        VIDEO_SELECT
        + """
        WHERE v.batch_id = %s AND v.status = 'pending'
        ORDER BY v.id ASC
        LIMIT 1 OFFSET %s
        """,
        (batch_id, safe_index),
    )
    if not row:
        send_batch_summary(tg, actor.chat_id, batch_id, edit_message_id)
        return
    acquire_admin_lock(int(row["id"]), actor)
    position = f"{safe_index + 1} из {total}"
    text = format_video_card(row, title="Заявка", position=position)
    keyboard = admin_video_keyboard(int(row["id"]), batch_id, safe_index, total)
    if edit_message_id:
        try:
            tg.edit_message_text(actor.chat_id, edit_message_id, text, keyboard)
            return
        except Exception:
            pass
    tg.send_message(actor.chat_id, text, keyboard)


def admin_video_keyboard(
    video_id: int,
    batch_id: int,
    index: int,
    total: int | None = None,
) -> dict[str, Any]:
    next_index = index + 1
    prev_index = max(index - 1, 0)
    if total is not None and next_index >= total:
        next_index = 0
    return inline_keyboard(
        [
            [("Указать дату", f"adm:date:{video_id}:{batch_id}:{index}")],
            [("Одобрить", f"adm:a:{video_id}:{batch_id}:{index}"), ("Правка", f"adm:r:{video_id}:{batch_id}:{index}")],
            [("Дубль", f"adm:d:{video_id}:{batch_id}:{index}"), ("Удалить", f"adm:x:{video_id}:{batch_id}:{index}")],
            [("Назад", f"adm:open:{batch_id}:{prev_index}"), ("Дальше", f"adm:open:{batch_id}:{next_index}")],
            [("К пачке", f"adm:sum:{batch_id}")],
        ]
    )


def show_admin_date_options(
    tg: TelegramClient,
    actor: Actor,
    video_id: int,
    batch_id: int,
    index: int,
    edit_message_id: int | None = None,
) -> None:
    if not require_admin(tg, actor):
        return
    text = "Выберите дату публикации или введите вручную."
    keyboard = inline_keyboard(
        [
            [("Сегодня", f"adm:setdate:{video_id}:{batch_id}:{index}:today")],
            [("Вчера", f"adm:setdate:{video_id}:{batch_id}:{index}:yesterday")],
            [("Позавчера", f"adm:setdate:{video_id}:{batch_id}:{index}:before_yesterday")],
            [("Ввести вручную", f"adm:manualdate:{video_id}:{batch_id}:{index}")],
            [("Назад", f"adm:open:{batch_id}:{index}")],
        ]
    )
    if edit_message_id:
        try:
            tg.edit_message_text(actor.chat_id, edit_message_id, text, keyboard)
            return
        except Exception:
            pass
    tg.send_message(actor.chat_id, text, keyboard)


def set_admin_date_preset(
    tg: TelegramClient,
    actor: Actor,
    video_id: int,
    batch_id: int,
    index: int,
    preset: str,
    edit_message_id: int | None = None,
) -> None:
    today = datetime.now(get_settings().tz).date()
    offsets = {
        "today": 0,
        "yesterday": 1,
        "before_yesterday": 2,
    }
    if preset not in offsets:
        tg.send_message(actor.chat_id, "Неизвестный вариант даты.")
        return
    publish_date = today - timedelta(days=offsets[preset])
    set_video_publish_date(tg, actor, video_id, batch_id, index, publish_date.isoformat(), edit_message_id)


def start_admin_manual_date(
    tg: TelegramClient,
    actor: Actor,
    video_id: int,
    batch_id: int,
    index: int,
) -> None:
    if not require_admin(tg, actor):
        return
    db.set_session(
        tg_id=actor.tg_id,
        chat_id=actor.chat_id,
        username=actor.username,
        state="admin:date",
        data={"video_id": video_id, "batch_id": batch_id, "index": index},
    )
    tg.send_message(actor.chat_id, "Введите дату публикации: YYYY-MM-DD, DD.MM или D.M.")


def handle_admin_date_message(
    tg: TelegramClient,
    actor: Actor,
    data: dict[str, Any],
    text: str,
) -> None:
    if not require_admin(tg, actor):
        return
    video_id = int(data.get("video_id") or 0)
    try:
        publish_date = parse_publish_date(text)
    except ValueError as exc:
        tg.send_message(actor.chat_id, str(exc))
        return
    error = _set_active_queue_publish_date(
        tg,
        actor,
        video_id,
        publish_date,
        int(data.get("active_message_chat_id") or actor.chat_id),
        int(data.get("active_message_id") or 0),
    )
    db.clear_session(actor.tg_id)
    if error:
        tg.send_message(
            actor.chat_id,
            f"Заявка #{video_id} уже не является текущей. Открой актуальную очередь: /admin",
        )


def set_video_publish_date(
    tg: TelegramClient,
    actor: Actor,
    video_id: int,
    batch_id: int,
    index: int,
    publish_date: str,
    edit_message_id: int | None = None,
) -> None:
    if not require_admin(tg, actor):
        return
    with db.transaction() as conn:
        before = get_video_by_id(conn, video_id)
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE videos
                SET publish_date = %s,
                    publish_date_set_by_tg_id = %s,
                    publish_date_set_by_username = %s,
                    publish_date_set_at = now(),
                    updated_at = now()
                WHERE id = %s AND status = 'pending'
                RETURNING id
                """,
                (publish_date, actor.tg_id, actor.username, video_id),
            )
            updated = cur.fetchone()
            if not updated:
                tg.send_message(actor.chat_id, "Заявка уже обработана или недоступна.")
                return
        recalculate_batch(conn, batch_id)
        db.log_event(
            conn,
            entity_type="video",
            entity_id=video_id,
            action="publish_date_set",
            actor_tg_id=actor.tg_id,
            actor_username=actor.username,
            before_data={"publish_date": before.get("publish_date") if before else None},
            after_data={"publish_date": publish_date},
        )
    formatted = parse_publish_date(publish_date).strftime("%d.%m.%Y")
    tg.send_message(actor.chat_id, f"Дата публикации установлена: {formatted}")
    show_queue_item(tg, actor, batch_id, index, edit_message_id)


def acquire_admin_lock(video_id: int, actor: Actor) -> None:
    try:
        with db.transaction() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM admin_locks WHERE locked_at < now() - interval '15 minutes'")
                cur.execute(
                    """
                    INSERT INTO admin_locks (video_id, admin_tg_id)
                    VALUES (%s, %s)
                    ON CONFLICT (video_id)
                    DO UPDATE SET
                        admin_tg_id = EXCLUDED.admin_tg_id,
                        locked_at = now()
                    WHERE admin_locks.admin_tg_id = EXCLUDED.admin_tg_id
                       OR admin_locks.locked_at < now() - interval '15 minutes'
                    """,
                    (video_id, actor.tg_id),
                )
    except Exception:
        pass


def approve_one(
    tg: TelegramClient,
    actor: Actor,
    video_id: int,
    batch_id: int,
    index: int,
    edit_message_id: int | None = None,
) -> None:
    if not require_admin(tg, actor):
        return
    current = get_video_by_id_outside(video_id)
    if not current or current.get("status") != "pending":
        tg.send_message(actor.chat_id, "Заявка уже обработана другим админом.")
        show_queue_item(tg, actor, batch_id, index, edit_message_id)
        return
    if not current.get("publish_date"):
        tg.send_message(actor.chat_id, "Сначала укажи дату публикации.")
        show_queue_item(tg, actor, batch_id, index, edit_message_id)
        return
    video = approve_video_in_db(video_id, actor)
    if not video:
        tg.send_message(actor.chat_id, "Заявка уже обработана другим админом.")
        show_queue_item(tg, actor, batch_id, index, edit_message_id)
        return
    sheet_ok = sync_video_after_approval(video, actor)
    send_admin_approved_card(tg, video, actor, edit_message_id)
    if not sheet_ok:
        send_admin_sync_warning(tg, video, actor)
    tg.send_message(actor.chat_id, f"Заявка #{video_id} одобрена.")
    next_edit_message_id = None
    current_message_is_admin_delivery = admin_delivery_message_matches(video, actor.chat_id, edit_message_id) or (
        bool(edit_message_id)
        and actor.chat_id == get_settings().admin_chat_id
        and not video.get("admin_message_id")
    )
    if edit_message_id and not current_message_is_admin_delivery:
        next_edit_message_id = edit_message_id
    show_queue_item(tg, actor, batch_id, index, next_edit_message_id)


def approve_video_in_db(video_id: int, actor: Actor) -> dict[str, Any] | None:
    with db.transaction() as conn:
        before = get_video_by_id(conn, video_id)
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE videos
                SET status = 'approved',
                    sheet_sync_status = 'queued',
                    sheet_sync_error = NULL,
                    checked_by_tg_id = %s,
                    checked_by_username = %s,
                    checked_at = now(),
                    updated_at = now()
                WHERE id = %s AND status = 'pending' AND publish_date IS NOT NULL
                RETURNING id
                """,
                (actor.tg_id, actor.username, video_id),
            )
            updated = cur.fetchone()
            if not updated:
                return None
            cur.execute("DELETE FROM admin_locks WHERE video_id = %s", (video_id,))
        video = get_video_by_id(conn, video_id)
        if video.get("batch_id"):
            recalculate_batch(conn, int(video["batch_id"]))
        db.log_event(
            conn,
            entity_type="video",
            entity_id=video_id,
            action="approved",
            actor_tg_id=actor.tg_id,
            actor_username=actor.username,
            before_data={"status": before.get("status")},
            after_data={"status": "approved"},
        )
        return video


def sync_video_after_approval(video: dict[str, Any], actor: Actor) -> bool:
    try:
        db.execute(
            """
            UPDATE videos
            SET sheet_sync_status = 'queued', sheet_sync_error = NULL, updated_at = now()
            WHERE id = %s
            """,
            (int(video["id"]),),
        )
        job_id = jobs.enqueue_sheet_sync(
            int(video["id"]),
            version=_sheet_sync_version(video),
        )
        record_system_log(
            "sheets_sync_queued",
            "video",
            int(video["id"]),
            {"job_id": job_id},
            actor,
        )
        return job_id is not None
    except Exception as exc:
        record_system_log(
            "sheets_sync_queue_failed",
            "video",
            int(video["id"]),
            {"error": _safe_error(exc)},
            actor,
        )
        return False


def _sheet_sync_version(video: dict[str, Any]) -> str:
    value = video.get("updated_at")
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    return str(value or "current")


def send_admin_approved_card(
    tg: TelegramClient,
    video: dict[str, Any],
    actor: Actor,
    fallback_message_id: int | None = None,
) -> bool:
    settings = get_settings()
    text = format_final_card(video)
    stored_chat_id = video.get("admin_message_chat_id")
    stored_message_id = video.get("admin_message_id")
    edit_chat_id = int(stored_chat_id) if stored_chat_id else None
    edit_message_id = int(stored_message_id) if stored_message_id else None

    if not edit_chat_id and actor.chat_id == settings.admin_chat_id and fallback_message_id:
        edit_chat_id = actor.chat_id
        edit_message_id = fallback_message_id

    if edit_chat_id and edit_message_id:
        try:
            tg.edit_message_text(edit_chat_id, edit_message_id, text, {"inline_keyboard": []})
            store_admin_message(
                int(video["id"]),
                int(edit_chat_id),
                {"result": {"message_id": int(edit_message_id)}},
            )
            return True
        except Exception as exc:
            record_system_log(
                "admin_notify_failed",
                "video",
                int(video["id"]),
                telegram_failure_payload(exc, int(settings.admin_chat_id), "edit_approved_card"),
                actor,
            )

    try:
        response = tg.send_message(settings.admin_chat_id, text)
        store_admin_message(int(video["id"]), int(settings.admin_chat_id), response)
        return True
    except Exception as exc:
        record_system_log(
            "admin_notify_failed",
            "video",
            int(video["id"]),
            telegram_failure_payload(exc, int(settings.admin_chat_id), "send_approved_card"),
            actor,
        )
        return False


def send_admin_sync_warning(tg: TelegramClient, video: dict[str, Any], actor: Actor) -> None:
    settings = get_settings()
    try:
        tg.send_message(
            settings.admin_chat_id,
            f"Заявка #{video['id']} одобрена, но Google Sheets не обновился. "
            "После исправления запустите /sync_sheets.",
        )
    except Exception as exc:
        record_system_log(
            "admin_notify_failed",
            "video",
            int(video["id"]),
            telegram_failure_payload(exc, int(settings.admin_chat_id), "send_sync_warning"),
            actor,
        )


def mark_video_status(
    tg: TelegramClient,
    actor: Actor,
    video_id: int,
    batch_id: int,
    index: int,
    status: str,
    user_message: str,
    edit_message_id: int | None = None,
) -> None:
    if not require_admin(tg, actor):
        return
    with db.transaction() as conn:
        before = get_video_by_id(conn, video_id)
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE videos
                SET status = %s,
                    checked_by_tg_id = %s,
                    checked_by_username = %s,
                    checked_at = CASE WHEN %s IN ('duplicate', 'deleted') THEN now() ELSE checked_at END,
                    updated_at = now()
                WHERE id = %s AND status = 'pending'
                RETURNING id, added_by_tg_id
                """,
                (status, actor.tg_id, actor.username, status, video_id),
            )
            updated = cur.fetchone()
            if not updated:
                tg.send_message(actor.chat_id, "Заявка уже обработана.")
                return
            cur.execute("DELETE FROM admin_locks WHERE video_id = %s", (video_id,))
        recalculate_batch(conn, batch_id)
        db.log_event(
            conn,
            entity_type="video",
            entity_id=video_id,
            action=status,
            actor_tg_id=actor.tg_id,
            actor_username=actor.username,
            before_data={"status": before.get("status")},
            after_data={"status": status},
        )
    try:
        if before.get("added_by_tg_id"):
            tg.send_message(before["added_by_tg_id"], f"{user_message}\nID: {video_id}")
    except Exception:
        pass
    tg.send_message(actor.chat_id, user_message)
    show_queue_item(tg, actor, batch_id, index, edit_message_id)


def approve_clean_batch(
    tg: TelegramClient,
    actor: Actor,
    batch_id: int,
    edit_message_id: int | None = None,
) -> None:
    if not require_admin(tg, actor):
        return
    rows = db.fetch_all(
        """
        SELECT id
        FROM videos
        WHERE batch_id = %s
          AND status = 'pending'
          AND publish_date IS NOT NULL
          AND (
            (COALESCE(video_type, 'regular') = 'regular' AND instagram_id IS NOT NULL)
            OR (COALESCE(video_type, 'regular') = 'bigrecap' AND youtube_id IS NOT NULL)
          )
          AND COALESCE(author_name, '') <> ''
          AND COALESCE(montage_name, '') <> ''
        ORDER BY id ASC
        """,
        (batch_id,),
    )
    approved = 0
    for row in rows:
        video = approve_video_in_db(int(row["id"]), actor)
        if not video:
            continue
        sheet_ok = sync_video_after_approval(video, actor)
        send_admin_approved_card(tg, video, actor)
        if not sheet_ok:
            send_admin_sync_warning(tg, video, actor)
        approved += 1
    tg.send_message(actor.chat_id, f"Одобрено чистых заявок: {approved}.")
    send_batch_summary(tg, actor.chat_id, batch_id, edit_message_id)


def show_summary(tg: TelegramClient, actor: Actor) -> None:
    if not require_admin(tg, actor):
        return
    rows = db.fetch_all(
        """
        SELECT status, count(*) AS count
        FROM videos
        GROUP BY status
        ORDER BY status
        """
    )
    today = datetime.now(get_settings().tz).date().isoformat()
    today_row = db.fetch_one(
        "SELECT count(*) AS count FROM videos WHERE publish_date = %s AND status = 'approved'",
        (today,),
    )
    type_rows = db.fetch_all(
        """
        SELECT COALESCE(video_type, 'regular') AS video_type, count(*) AS count
        FROM videos
        GROUP BY COALESCE(video_type, 'regular')
        ORDER BY video_type
        """
    )
    lines = ["Сводка:"]
    for row in rows:
        lines.append(f"{row['status']}: {row['count']}")
    if type_rows:
        lines.append("")
        lines.append("Типы:")
        for row in type_rows:
            label = "большие рекапы" if row["video_type"] == "bigrecap" else "ролики"
            lines.append(f"{label}: {row['count']}")
    lines.append(f"approved сегодня ({today}): {(today_row or {}).get('count', 0)}")
    tg.send_message(actor.chat_id, "\n".join(lines))


def show_calendar(tg: TelegramClient, actor: Actor) -> None:
    if not require_admin(tg, actor):
        return
    rows = db.fetch_all(
        """
        SELECT publish_date, status, count(*) AS count
        FROM videos
        WHERE publish_date >= current_date - interval '3 days'
          AND publish_date <= current_date + interval '30 days'
        GROUP BY publish_date, status
        ORDER BY publish_date, status
        """
    )
    if not rows:
        tg.send_message(actor.chat_id, "Календарь пуст.")
        return
    lines = ["Календарь:"]
    for row in rows:
        lines.append(f"{row['publish_date']}: {row['status']} — {row['count']}")
    tg.send_message(actor.chat_id, "\n".join(lines))


def show_people(tg: TelegramClient, actor: Actor) -> None:
    if not require_admin(tg, actor):
        return
    rows = db.fetch_all(
        """
        SELECT role, count(*) FILTER (WHERE is_active) AS active_count,
               count(*) FILTER (WHERE NOT is_active) AS inactive_count
        FROM people
        GROUP BY role
        ORDER BY role
        """
    )
    lines = ["Участники:"]
    for row in rows:
        lines.append(f"{row['role']}: активных {row['active_count']}, выключенных {row['inactive_count']}")
    if is_superadmin(actor.tg_id):
        lines.extend(
            [
                "",
                "Управление:",
                "/add_person role name [tg_id] [@username]",
                "/activate_person id",
                "/deactivate_person id",
            ]
        )
    tg.send_message(actor.chat_id, "\n".join(lines))


def start_person_lookup(tg: TelegramClient, actor: Actor) -> None:
    db.set_session(
        tg_id=actor.tg_id,
        chat_id=actor.chat_id,
        username=actor.username,
        state="admin:person",
        data={},
    )
    tg.send_message(actor.chat_id, "Введите @username, Telegram ID, people.id или точное имя участника.")


def _person_identity_key(row: dict[str, Any]) -> tuple[str, str]:
    if row.get("tg_id") is not None:
        return "tg", str(row["tg_id"])
    if row.get("username"):
        return "username", str(row["username"]).lower()
    return "name", str(row.get("name") or "").lower()


def find_person_candidates(query: str) -> list[dict[str, Any]]:
    raw = query.strip()
    normalized = raw.lstrip("@").strip()
    if not normalized:
        return []
    if normalized.isdigit():
        rows = db.fetch_all(
            """
            SELECT id, name, username, tg_id, role, is_active
            FROM people
            WHERE id = %s OR tg_id = %s
            ORDER BY is_active DESC, id
            """,
            (int(normalized), int(normalized)),
        )
    else:
        rows = db.fetch_all(
            """
            SELECT id, name, username, tg_id, role, is_active
            FROM people
            WHERE lower(username) = lower(%s) OR lower(name) = lower(%s)
            ORDER BY is_active DESC, id
            """,
            (normalized, normalized),
        )
    identities: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        identities.setdefault(_person_identity_key(row), row)
    return list(identities.values())


def _load_person_identity(conn, person_id: int) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, name, username, tg_id, role, is_active FROM people WHERE id = %s",
            (person_id,),
        )
        selected = cur.fetchone()
        if not selected:
            return None
        if selected.get("tg_id") is not None:
            cur.execute("SELECT id FROM people WHERE tg_id = %s ORDER BY id", (selected["tg_id"],))
        elif selected.get("username"):
            cur.execute(
                "SELECT id FROM people WHERE lower(username) = lower(%s) ORDER BY id",
                (selected["username"],),
            )
        else:
            cur.execute(
                "SELECT id FROM people WHERE lower(name) = lower(%s) ORDER BY id",
                (selected["name"],),
            )
        identity_ids = [int(row["id"]) for row in cur.fetchall()]
    return {**selected, "identity_ids": identity_ids}


def _person_role_condition(role: str, identity: dict[str, Any]) -> tuple[str, tuple[Any, ...]]:
    clauses = [f"v.{role}_id = ANY(%s)"]
    params: list[Any] = [identity["identity_ids"]]
    if identity.get("username"):
        clauses.append(f"lower(v.{role}_username) = lower(%s)")
        params.append(identity["username"])
    elif identity.get("name"):
        clauses.append(f"lower(v.{role}_name) = lower(%s)")
        params.append(identity["name"])
    return "(" + " OR ".join(clauses) + ")", tuple(params)


def _person_any_condition(identity: dict[str, Any]) -> tuple[str, tuple[Any, ...]]:
    clauses: list[str] = []
    params: list[Any] = []
    for role in ("author", "montage", "voice"):
        clause, role_params = _person_role_condition(role, identity)
        clauses.append(clause)
        params.extend(role_params)
    return "(" + " OR ".join(clauses) + ")", tuple(params)


def load_person_profile(person_id: int, *, offset: int = 0, limit: int = 5) -> dict[str, Any] | None:
    settings = get_settings()
    now = datetime.now(settings.tz)
    month_start_local = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if month_start_local.month == 12:
        next_month_local = month_start_local.replace(year=month_start_local.year + 1, month=1)
    else:
        next_month_local = month_start_local.replace(month=month_start_local.month + 1)
    month_start = month_start_local.astimezone(timezone.utc)
    next_month = next_month_local.astimezone(timezone.utc)
    with db.connect() as conn:
        identity = _load_person_identity(conn, person_id)
        if not identity:
            return None
        role_counts: dict[str, dict[str, int]] = {}
        with conn.cursor() as cur:
            for role in ("author", "montage", "voice"):
                condition, params = _person_role_condition(role, identity)
                cur.execute(
                    f"""
                    SELECT
                        count(*) FILTER (WHERE v.status = 'approved') AS all_count,
                        count(*) FILTER (
                            WHERE v.status = 'approved'
                              AND v.checked_at >= %s
                              AND v.checked_at < %s
                        ) AS month_count
                    FROM videos v
                    WHERE {condition}
                    """,
                    (month_start, next_month, *params),
                )
                row = cur.fetchone()
                role_counts[role] = {
                    "all": int(row["all_count"] or 0),
                    "month": int(row["month_count"] or 0),
                }
            any_condition, any_params = _person_any_condition(identity)
            cur.execute(
                f"SELECT count(*) AS count FROM videos v WHERE v.status = 'pending' AND {any_condition}",
                any_params,
            )
            pending_count = int(cur.fetchone()["count"])
            cur.execute(
                f"""
                SELECT
                    COALESCE(project_code, 'unassigned') AS project_code,
                    COALESCE(NULLIF(project_name, ''), 'Без проекта') AS project_name,
                    count(*) AS count
                FROM videos v
                WHERE v.status = 'approved' AND {any_condition}
                GROUP BY
                    COALESCE(project_code, 'unassigned'),
                    COALESCE(NULLIF(project_name, ''), 'Без проекта')
                ORDER BY count(*) DESC, project_name
                """,
                any_params,
            )
            projects = [
                {
                    "project_code": row["project_code"],
                    "project_name": row["project_name"],
                    "count": int(row["count"]),
                }
                for row in cur.fetchall()
            ]
            cur.execute(
                VIDEO_SELECT
                + f"""
                WHERE v.status <> 'deleted' AND {any_condition}
                ORDER BY COALESCE(v.publish_date, v.created_at::date) DESC, v.created_at DESC, v.id DESC
                LIMIT %s OFFSET %s
                """,
                (*any_params, limit + 1, max(0, offset)),
            )
            video_rows = list(cur.fetchall())
    return {
        "id": int(identity["id"]),
        "name": identity["name"],
        "username": identity.get("username"),
        "tg_id": identity.get("tg_id"),
        "role_counts": role_counts,
        "pending_count": pending_count,
        "projects": projects,
        "videos": video_rows[:limit],
        "has_more": len(video_rows) > limit,
        "offset": max(0, offset),
        "page_size": limit,
    }


def _profile_name(profile: dict[str, Any]) -> str:
    username = profile.get("username")
    return f"{profile['name']} (@{username})" if username else str(profile["name"])


def _video_display_date(video: dict[str, Any]) -> str:
    value = video.get("publish_date") or video.get("created_at")
    if isinstance(value, datetime):
        value = value.astimezone(get_settings().tz).date()
    return value.strftime("%d.%m.%Y") if isinstance(value, date) else "дата не указана"


def format_person_profile(profile: dict[str, Any]) -> str:
    counts = profile["role_counts"]
    lines = [
        f"👤 {_profile_name(profile)}",
        "",
        "За всё время:",
        f"Автор — {counts['author']['all']}",
        f"Монтаж — {counts['montage']['all']}",
        f"Озвучка — {counts['voice']['all']}",
        "",
        "За текущий месяц:",
        f"Автор — {counts['author']['month']}",
        f"Монтаж — {counts['montage']['month']}",
        f"Озвучка — {counts['voice']['month']}",
        "",
        f"Ожидают проверки: {profile['pending_count']}",
    ]
    if profile["projects"]:
        lines.extend(["", "По проектам:"])
        lines.extend(
            f"{row['project_name']} — {row['count']}" for row in profile["projects"][:8]
        )
    if profile["videos"]:
        video = profile["videos"][0]
        lines.extend(
            [
                "",
                "Последний ролик:",
                f"#{video['id']} — {_video_display_date(video)}",
                f"Проект: {video.get('project_name') or 'не указан'}",
            ]
        )
    return "\n".join(lines)


def person_profile_keyboard(person_id: int) -> dict[str, Any]:
    return inline_keyboard(
        [
            [("🎬 Последние ролики", f"person:videos:{person_id}:0")],
            [("📂 Все проекты", f"person:projects:{person_id}")],
        ]
    )


def show_person_profile(tg: TelegramClient, actor: Actor, person_id: int) -> None:
    profile = load_person_profile(person_id)
    if not profile:
        tg.send_message(actor.chat_id, "Участник не найден.")
        return
    tg.send_message(actor.chat_id, format_person_profile(profile), person_profile_keyboard(profile["id"]))
    record_system_log(
        "person_profile_viewed",
        "person",
        int(profile["id"]),
        {"query_person_id": person_id},
        actor,
    )


def person_command(tg: TelegramClient, actor: Actor, query: str) -> None:
    if not require_admin(tg, actor):
        return
    if not query.strip():
        start_person_lookup(tg, actor)
        return
    candidates = find_person_candidates(query)
    if not candidates:
        tg.send_message(actor.chat_id, "Участник не найден.")
        return
    if len(candidates) == 1:
        show_person_profile(tg, actor, int(candidates[0]["id"]))
        return
    buttons = [
        [
            (
                f"{row['name']} (@{row['username']})" if row.get("username") else f"{row['name']} · ID {row['id']}",
                f"person:view:{row['id']}",
            )
        ]
        for row in candidates[:10]
    ]
    tg.send_message(actor.chat_id, "Найдено несколько участников. Выберите нужного:", inline_keyboard(buttons))


def _format_person_videos(profile: dict[str, Any]) -> str:
    page = profile["offset"] // profile["page_size"] + 1
    lines = [f"🎬 Ролики: {_profile_name(profile)}", f"Страница {page}"]
    if not profile["videos"]:
        lines.extend(["", "Роликов на этой странице нет."])
    for video in profile["videos"]:
        lines.extend(
            [
                "",
                f"#{video['id']} — {_video_display_date(video)}",
                f"Статус: {video.get('status')}",
                f"Проект: {video.get('project_name') or 'не указан'}",
            ]
        )
    return "\n".join(lines)


def _person_videos_keyboard(profile: dict[str, Any]) -> dict[str, Any]:
    person_id = int(profile["id"])
    offset = int(profile["offset"])
    page_size = int(profile["page_size"])
    navigation: list[tuple[str, str]] = []
    if offset > 0:
        navigation.append(("⬅️", f"person:videos:{person_id}:{max(0, offset - page_size)}"))
    if profile.get("has_more"):
        navigation.append(("➡️", f"person:videos:{person_id}:{offset + page_size}"))
    rows = [navigation] if navigation else []
    rows.append([("↩️ К профилю", f"person:view:{person_id}")])
    return inline_keyboard(rows)


def handle_person_profile_callback(
    tg: TelegramClient,
    actor: Actor,
    data: str,
    callback_id: str,
) -> None:
    if not is_admin(actor.tg_id):
        _answer_queue_callback(tg, callback_id, "Это действие доступно только админам.", show_alert=True)
        return
    try:
        parts = data.split(":")
        action = parts[1]
        person_id = int(parts[2])
        if action == "view":
            show_person_profile(tg, actor, person_id)
        elif action == "videos":
            offset = max(0, int(parts[3]))
            profile = load_person_profile(person_id, offset=offset)
            if not profile:
                raise ValueError("person not found")
            tg.send_message(actor.chat_id, _format_person_videos(profile), _person_videos_keyboard(profile))
        elif action == "projects":
            profile = load_person_profile(person_id)
            if not profile:
                raise ValueError("person not found")
            lines = [f"📂 Проекты: {_profile_name(profile)}", ""]
            lines.extend(
                f"{row['project_name']} — {row['count']}" for row in profile["projects"]
            )
            if not profile["projects"]:
                lines.append("Одобренных роликов пока нет.")
            tg.send_message(
                actor.chat_id,
                "\n".join(lines),
                inline_keyboard([[('↩️ К профилю', f"person:view:{person_id}")]]),
            )
        else:
            raise ValueError("unknown person callback")
        _answer_queue_callback(tg, callback_id)
    except (IndexError, TypeError, ValueError):
        _answer_queue_callback(tg, callback_id, "Карточка участника устарела.", show_alert=True)


def start_admin_search(tg: TelegramClient, actor: Actor) -> None:
    db.set_session(
        tg_id=actor.tg_id,
        chat_id=actor.chat_id,
        username=actor.username,
        state="admin:search",
        data={},
    )
    record_system_log("admin_search_started", "admin_search", None, {"source": "prompt"}, actor)
    tg.send_message(actor.chat_id, "Введите ID, shortcode, ссылку, @username или точное имя.")


def start_or_run_search(tg: TelegramClient, actor: Actor, query: str) -> None:
    if not require_admin(tg, actor):
        return
    if query:
        run_search(tg, actor, query)
        return
    start_admin_search(tg, actor)


def _external_id(raw: str, normalizer) -> str | None:
    try:
        return normalizer(raw).external_id
    except Exception:
        return None


def find_videos_exact(query: str) -> tuple[str, list[dict[str, Any]]]:
    q = query.strip()
    if not q:
        return "empty", []
    if q.isdigit():
        rows = db.fetch_all(VIDEO_SELECT + " WHERE v.id = %s LIMIT 1", (int(q),))
        if rows:
            return "video_id", rows

    plain_token = q if "://" not in q and " " not in q else None
    candidates = [
        ("instagram_id", _external_id(q, normalize_instagram) or plain_token),
        ("youtube_id", _external_id(q, normalize_youtube) or plain_token),
        ("tiktok_id", _external_id(q, normalize_tiktok) or plain_token),
        ("vk_id", _external_id(q, normalize_vk) or plain_token),
    ]
    for column, value in candidates:
        if not value:
            continue
        rows = db.fetch_all(
            VIDEO_SELECT + f" WHERE v.{column} = %s ORDER BY v.created_at DESC LIMIT 10",
            (value,),
        )
        if rows:
            return column, rows

    person_query = q.lstrip("@").strip()
    rows = db.fetch_all(
        VIDEO_SELECT
        + """
        WHERE lower(v.author_username) = lower(%s)
           OR lower(v.montage_username) = lower(%s)
           OR lower(v.voice_username) = lower(%s)
        ORDER BY v.created_at DESC
        LIMIT 10
        """,
        (person_query, person_query, person_query),
    )
    if rows:
        return "username", rows

    rows = db.fetch_all(
        VIDEO_SELECT
        + """
        WHERE lower(v.author_name) = lower(%s)
           OR lower(v.montage_name) = lower(%s)
           OR lower(v.voice_name) = lower(%s)
        ORDER BY v.created_at DESC
        LIMIT 10
        """,
        (q, q, q),
    )
    if rows:
        return "name", rows

    like = f"%{q}%"
    rows = db.fetch_all(
        VIDEO_SELECT
        + """
        WHERE v.instagram_url ILIKE %s
           OR v.youtube_url ILIKE %s
           OR v.tiktok_url ILIKE %s
           OR v.vk_url ILIKE %s
        ORDER BY v.created_at DESC
        LIMIT 10
        """,
        (like, like, like, like),
    )
    return "url_substring", rows


def format_search_result(video: dict[str, Any]) -> str:
    lines = [
        f"🔎 Заявка #{video['id']}",
        f"Статус: {video.get('status') or 'не указан'}",
        f"Проект: {video.get('project_name') or 'не указан'}",
        f"Дата: {_video_display_date(video)}",
        "",
        "Ссылки:",
    ]
    links = [
        ("Instagram", video.get("instagram_url")),
        ("YouTube", video.get("youtube_url")),
        ("TikTok", video.get("tiktok_url")),
        ("VK", video.get("vk_url")),
    ]
    lines.extend(f"{label}: {value}" for label, value in links if value)
    if not any(value for _, value in links):
        lines.append("нет")
    lines.extend(
        [
            "",
            "Участники:",
            f"Автор: {person_value(video, 'author')}",
            f"Монтаж: {person_value(video, 'montage')}",
            f"Озвучка: {person_value(video, 'voice') if video.get('voice_name') else 'нет'}",
        ]
    )
    return "\n".join(lines)


def run_search(tg: TelegramClient, actor: Actor, query: str) -> None:
    q = query.strip()
    record_system_log("admin_search_started", "admin_search", None, {"query": q[:100]}, actor)
    stage, rows = find_videos_exact(q)
    record_system_log(
        "admin_search_result",
        "admin_search",
        int(rows[0]["id"]) if len(rows) == 1 else None,
        {"query": q[:100], "stage": stage, "count": len(rows)},
        actor,
    )
    if not rows:
        tg.send_message(actor.chat_id, "Ничего не найдено.")
        return
    for row in rows:
        tg.send_message(actor.chat_id, format_search_result(row))


def _parse_daily_report_date(raw: str) -> date:
    if not raw.strip():
        return previous_report_date()
    try:
        return date.fromisoformat(raw.strip())
    except ValueError as exc:
        raise ValueError("Используйте дату в формате YYYY-MM-DD.") from exc


def daily_report_command(tg: TelegramClient, actor: Actor, raw_date: str) -> None:
    if not require_admin(tg, actor):
        return
    try:
        report_date = _parse_daily_report_date(raw_date)
        _, text = preview_daily_report(report_date)
        tg.send_message(
            actor.tg_id,
            "Предпросмотр перед отправкой в admin chat:\n\n" + text,
            inline_keyboard([[('📤 Отправить отчёт', f"daily:send:{report_date.isoformat()}")]]),
        )
        if actor.chat_id != actor.tg_id:
            tg.send_message(actor.chat_id, "Предпросмотр отправлен вам в личку.")
    except ValueError as exc:
        tg.send_message(actor.chat_id, str(exc))
    except Exception:
        tg.send_message(actor.chat_id, "Не удалось отправить preview в личку. Сначала откройте диалог с ботом.")


def handle_daily_report_callback(
    tg: TelegramClient,
    actor: Actor,
    data: str,
    callback_id: str,
) -> None:
    if not is_admin(actor.tg_id):
        _answer_queue_callback(tg, callback_id, "Это действие доступно только админам.", show_alert=True)
        return
    try:
        _, action, raw_date = data.split(":", 2)
        if action != "send":
            raise ValueError("unknown daily report action")
        report_date = date.fromisoformat(raw_date)
        job_id = jobs.enqueue_job(
            "daily_report",
            {
                "report_date": report_date.isoformat(),
                "actor_tg_id": actor.tg_id,
                "actor_username": actor.username,
            },
            dedupe_key=f"daily-report:{report_date.isoformat()}",
            priority=70,
        )
        text = "Отчёт поставлен в очередь." if job_id else "Фоновые задания временно отключены."
        _answer_queue_callback(tg, callback_id, text, show_alert=True)
    except (TypeError, ValueError):
        _answer_queue_callback(tg, callback_id, "Кнопка отчёта устарела.", show_alert=True)
    except Exception:
        _answer_queue_callback(tg, callback_id, "Не удалось отправить отчёт.", show_alert=True)


def sync_sheets_command(tg: TelegramClient, actor: Actor) -> None:
    if not require_admin(tg, actor):
        return
    rows = db.fetch_all(
        VIDEO_SELECT
        + """
        WHERE v.status = 'approved'
        ORDER BY v.updated_at DESC
        LIMIT 200
        """
    )
    queued = 0
    for row in rows:
        queued += int(
            jobs.enqueue_sheet_sync(int(row["id"]), version=_sheet_sync_version(row)) is not None
        )
    jobs.enqueue_job("sheets_sync_stats", {}, dedupe_key="stats:projects", priority=80)
    tg.send_message(actor.chat_id, f"Синхронизация поставлена в очередь. Видео: {queued}.")


def sync_youtube_metrics_command(tg: TelegramClient, actor: Actor) -> None:
    if not require_admin(tg, actor):
        return
    job_id = jobs.enqueue_job(
        "youtube_metrics",
        {"actor_tg_id": actor.tg_id, "actor_username": actor.username},
        dedupe_key=f"youtube-metrics:{date.today().isoformat()}",
        priority=70,
    )
    text = "Обновление YouTube-метрик поставлено в очередь." if job_id else "Фоновые задания временно отключены."
    tg.send_message(actor.chat_id, text)


def metrics_youtube_today_command(tg: TelegramClient, actor: Actor) -> None:
    if not require_admin(tg, actor):
        return
    tg.send_message(actor.chat_id, metrics.format_youtube_today())


def metrics_youtube_all_command(tg: TelegramClient, actor: Actor) -> None:
    if not require_admin(tg, actor):
        return
    tg.send_message(actor.chat_id, metrics.format_youtube_all())


def metrics_video_command(tg: TelegramClient, actor: Actor, rest: str) -> None:
    if not require_admin(tg, actor):
        return
    value = rest.strip()
    if not value.isdigit():
        tg.send_message(actor.chat_id, "Формат: /metrics_video <video_id>")
        return
    tg.send_message(actor.chat_id, metrics.format_video_metrics(int(value)))


def start_add_links(tg: TelegramClient, actor: Actor, video_id: int) -> None:
    video = get_video_by_id_outside(video_id)
    if not video:
        tg.send_message(actor.chat_id, "Заявка не найдена.")
        return
    if video.get("added_by_tg_id") != actor.tg_id and not is_admin(actor.tg_id):
        tg.send_message(actor.chat_id, "Можно дополнять только свои заявки.")
        return
    db.set_session(
        tg_id=actor.tg_id,
        chat_id=actor.chat_id,
        username=actor.username,
        state="links:youtube",
        data={"video_id": video_id, "links": {}},
    )
    tg.send_message(actor.chat_id, "Пришлите YouTube ссылку или пропустите.", inline_keyboard([[("Пропустить", "skip:youtube")]]))


def handle_add_links_message(
    tg: TelegramClient,
    actor: Actor,
    data: dict[str, Any],
    platform: str,
    text: str,
) -> None:
    links = data.get("links") or {}
    try:
        link = normalize_optional(platform, text)
    except ValueError:
        tg.send_message(actor.chat_id, "Не удалось разобрать ссылку. Пришлите её ещё раз или нажмите «Пропустить».")
        return
    if link:
        links[f"{platform}_url"] = link.url
        links[f"{platform}_id"] = link.external_id
    data["links"] = links

    if platform == "youtube":
        state = "links:tiktok"
        prompt = "Пришлите TikTok ссылку или пропустите."
        callback = "skip:tiktok"
    elif platform == "tiktok":
        state = "links:vk"
        prompt = "Пришлите VK ссылку или пропустите."
        callback = "skip:vk"
    else:
        finish_add_links(tg, actor, data)
        return

    db.set_session(
        tg_id=actor.tg_id,
        chat_id=actor.chat_id,
        username=actor.username,
        state=state,
        data=data,
    )
    tg.send_message(actor.chat_id, prompt, inline_keyboard([[("Пропустить", callback)]]))


def finish_add_links(tg: TelegramClient, actor: Actor, data: dict[str, Any]) -> None:
    video_id = int(data["video_id"])
    links = data.get("links") or {}
    if not links:
        db.clear_session(actor.tg_id)
        tg.send_message(actor.chat_id, "Новые ссылки не добавлены.")
        return
    with db.transaction() as conn:
        before = get_video_by_id(conn, video_id)
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE videos
                SET youtube_url = COALESCE(%s, youtube_url),
                    youtube_id = COALESCE(%s, youtube_id),
                    tiktok_url = COALESCE(%s, tiktok_url),
                    tiktok_id = COALESCE(%s, tiktok_id),
                    vk_url = COALESCE(%s, vk_url),
                    vk_id = COALESCE(%s, vk_id),
                    updated_at = now()
                WHERE id = %s
                RETURNING id
                """,
                (
                    links.get("youtube_url"),
                    links.get("youtube_id"),
                    links.get("tiktok_url"),
                    links.get("tiktok_id"),
                    links.get("vk_url"),
                    links.get("vk_id"),
                    video_id,
                ),
            )
        after = get_video_by_id(conn, video_id)
        db.log_event(
            conn,
            entity_type="video",
            entity_id=video_id,
            action="links_updated",
            actor_tg_id=actor.tg_id,
            actor_username=actor.username,
            before_data={
                "youtube_url": before.get("youtube_url"),
                "tiktok_url": before.get("tiktok_url"),
                "vk_url": before.get("vk_url"),
            },
            after_data=links,
        )
    db.clear_session(actor.tg_id)
    if after.get("status") == "approved":
        if not sync_video_after_approval(after, actor):
            send_admin_sync_warning(tg, after, actor)
    tg.send_message(actor.chat_id, "Ссылки обновлены.")


def add_person_command(tg: TelegramClient, actor: Actor, rest: str) -> None:
    if not require_superadmin(tg, actor):
        return
    parts = rest.split()
    if len(parts) < 2:
        tg.send_message(actor.chat_id, "Формат: /add_person role name [tg_id] [@username]")
        return
    role = parts[0]
    if role not in {"author", "montage", "voice", "admin", "superadmin"}:
        tg.send_message(actor.chat_id, "Неизвестная роль.")
        return
    username = None
    tg_id = None
    name_parts: list[str] = []
    for item in parts[1:]:
        if item.startswith("@"):
            username = item[1:]
        elif item.lstrip("-").isdigit():
            tg_id = int(item)
        else:
            name_parts.append(item)
    name = " ".join(name_parts).strip()
    if not name:
        tg.send_message(actor.chat_id, "Укажите имя.")
        return
    with db.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO people (name, tg_id, username, role, is_active)
                VALUES (%s, %s, %s, %s, true)
                RETURNING id
                """,
                (name, tg_id, username, role),
            )
            person_id = int(cur.fetchone()["id"])
        db.log_event(
            conn,
            entity_type="person",
            entity_id=person_id,
            action="person_added",
            actor_tg_id=actor.tg_id,
            actor_username=actor.username,
            after_data={"role": role, "name": name},
        )
    tg.send_message(actor.chat_id, f"Добавлен участник #{person_id}: {name} ({role}).")


def set_person_active_command(tg: TelegramClient, actor: Actor, rest: str, active: bool) -> None:
    if not require_superadmin(tg, actor):
        return
    if not rest.strip().isdigit():
        tg.send_message(actor.chat_id, "Укажите ID участника.")
        return
    person_id = int(rest.strip())
    with db.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE people SET is_active = %s WHERE id = %s RETURNING id, name",
                (active, person_id),
            )
            row = cur.fetchone()
        if row:
            db.log_event(
                conn,
                entity_type="person",
                entity_id=person_id,
                action="person_activated" if active else "person_deactivated",
                actor_tg_id=actor.tg_id,
                actor_username=actor.username,
                after_data={"is_active": active},
            )
    tg.send_message(actor.chat_id, "Готово." if row else "Участник не найден.")


def edit_video_command(tg: TelegramClient, actor: Actor, rest: str) -> None:
    if not require_admin(tg, actor):
        return
    parts = rest.split(maxsplit=2)
    if len(parts) < 3 or not parts[0].isdigit():
        tg.send_message(actor.chat_id, "Формат: /edit_video id field value")
        return
    video_id = int(parts[0])
    field = parts[1]
    value = parts[2].strip()
    allowed = {
        "publish_date",
        "youtube_url",
        "tiktok_url",
        "vk_url",
        "video_type",
        "author_name",
        "montage_name",
        "voice_name",
        "comment",
    }
    if field not in allowed:
        tg.send_message(actor.chat_id, "Поле нельзя редактировать этой командой.")
        return
    update_field = field
    update_value: Any = value
    extra_field = None
    extra_value = None
    try:
        if field == "publish_date":
            update_value = parse_publish_date(value).isoformat()
        elif field == "youtube_url":
            link = normalize_optional("youtube", value)
            update_value = link.url if link else None
            extra_field = "youtube_id"
            extra_value = link.external_id if link else None
        elif field == "tiktok_url":
            link = normalize_optional("tiktok", value)
            update_value = link.url if link else None
            extra_field = "tiktok_id"
            extra_value = link.external_id if link else None
        elif field == "vk_url":
            link = normalize_optional("vk", value)
            update_value = link.url if link else None
            extra_field = "vk_id"
            extra_value = link.external_id if link else None
        elif field == "video_type":
            update_value = value.strip().lower()
            if update_value not in VIDEO_TYPES:
                tg.send_message(actor.chat_id, "video_type должен быть regular или bigrecap.")
                return
    except Exception as exc:
        tg.send_message(actor.chat_id, _safe_error(exc))
        return

    with db.transaction() as conn:
        before = get_video_by_id(conn, video_id)
        with conn.cursor() as cur:
            if extra_field:
                cur.execute(
                    f"UPDATE videos SET {update_field} = %s, {extra_field} = %s, updated_at = now() WHERE id = %s",
                    (update_value, extra_value, video_id),
                )
            else:
                cur.execute(
                    f"UPDATE videos SET {update_field} = %s, updated_at = now() WHERE id = %s",
                    (update_value, video_id),
                )
        after = get_video_by_id(conn, video_id)
        db.log_event(
            conn,
            entity_type="video",
            entity_id=video_id,
            action="video_edited",
            actor_tg_id=actor.tg_id,
            actor_username=actor.username,
            before_data={field: before.get(field)},
            after_data={field: update_value},
        )
    if after.get("status") == "approved":
        if not sync_video_after_approval(after, actor):
            send_admin_sync_warning(tg, after, actor)
    tg.send_message(actor.chat_id, "Запись обновлена.")


def record_system_log(
    action: str,
    entity_type: str,
    entity_id: int | None,
    after_data: dict[str, Any],
    actor: Actor | None = None,
) -> None:
    try:
        with db.transaction() as conn:
            db.log_event(
                conn,
                entity_type=entity_type,
                entity_id=entity_id,
                action=action,
                actor_tg_id=actor.tg_id if actor else None,
                actor_username=actor.username if actor else None,
                after_data=after_data,
            )
    except Exception:
        pass
