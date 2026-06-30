from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import psycopg

from bot import db, sheets
from bot.config import get_settings
from bot.links import (
    is_skip_text,
    normalize_instagram,
    normalize_optional,
    parse_publish_date,
)
from bot.messages import format_batch_summary, format_final_card, format_video_card
from bot.telegram import TelegramClient, inline_keyboard


ROLE_BY_SHORT = {"a": "author", "m": "montage", "v": "voice"}
SHORT_BY_ROLE = {value: key for key, value in ROLE_BY_SHORT.items()}
PERSON_USAGE_COLUMN = {
    "author": "author_id",
    "montage": "montage_id",
    "voice": "voice_id",
}

VIDEO_SELECT = """
SELECT
    v.*,
    COALESCE(v.author_name, author_p.name) AS author_name,
    author_p.tg_id AS author_tg_id,
    COALESCE(v.montage_name, montage_p.name) AS montage_name,
    montage_p.tg_id AS montage_tg_id,
    COALESCE(v.voice_name, voice_p.name) AS voice_name,
    voice_p.tg_id AS voice_tg_id
FROM videos v
LEFT JOIN people author_p ON author_p.id = v.author_id
LEFT JOIN people montage_p ON montage_p.id = v.montage_id
LEFT JOIN people voice_p ON voice_p.id = v.voice_id
"""


@dataclass(frozen=True)
class Actor:
    tg_id: int
    chat_id: int
    username: str | None = None
    first_name: str | None = None


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
    )


def _safe_error(exc: Exception) -> str:
    text = str(exc).replace(get_settings().bot_token or "", "[token]")
    return text[:500]


def _command_parts(text: str) -> tuple[str, str]:
    first, _, rest = text.strip().partition(" ")
    command = first.split("@", 1)[0].lower()
    return command, rest.strip()


def _send_main_menu(tg: TelegramClient, actor: Actor, text: str) -> None:
    rows = [
        [("Новое видео", "cmd:new"), ("Мои заявки", "cmd:my")],
        [("Помощь", "cmd:help")],
    ]
    if is_admin(actor.tg_id):
        rows.insert(1, [("Админка", "cmd:admin"), ("Сводка", "cmd:summary")])
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
        elif command == "/my_requests":
            show_my_requests(tg, actor)
        elif command == "/admin":
            show_admin(tg, actor)
        elif command == "/summary":
            show_summary(tg, actor)
        elif command == "/calendar":
            show_calendar(tg, actor)
        elif command == "/people":
            show_people(tg, actor)
        elif command == "/search":
            start_or_run_search(tg, actor, rest)
        elif command == "/sync_sheets":
            sync_sheets_command(tg, actor)
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

    try:
        tg.answer_callback_query(callback["id"])
    except Exception:
        pass

    if data == "cmd:new":
        start_new_video(tg, actor)
    elif data == "cmd:my":
        show_my_requests(tg, actor)
    elif data == "cmd:admin":
        show_admin(tg, actor)
    elif data == "cmd:summary":
        show_summary(tg, actor)
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
    elif data.startswith("p:"):
        _, short_role, raw_person_id = data.split(":", 2)
        handle_person_pick(tg, actor, short_role, int(raw_person_id))
    elif data.startswith("pm:"):
        _, short_role = data.split(":", 1)
        ask_manual_person(tg, actor, short_role)
    elif data == "vn":
        handle_voice_none(tg, actor)
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
    text = "\n".join(
        [
            "Команды:",
            "/new_video — добавить Reels",
            "/my_requests — мои заявки и дополнение ссылок",
            "/admin — очередь проверки",
            "/summary — сводка для админов",
            "/calendar — календарь публикаций",
            "/people — участники",
            "/search — поиск",
            "/sync_sheets — повторная синхронизация Google Sheets",
            "",
            "Для суперадминов:",
            "/add_person role name [tg_id] [@username]",
            "/activate_person id",
            "/deactivate_person id",
            "",
            "Роли: author, montage, voice, admin, superadmin.",
        ]
    )
    tg.send_message(actor.chat_id, text)


def start_new_video(tg: TelegramClient, actor: Actor) -> None:
    db.set_session(
        tg_id=actor.tg_id,
        chat_id=actor.chat_id,
        username=actor.username,
        state="new:instagram",
        data={},
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
    elif state == "admin:date":
        handle_admin_date_message(tg, actor, data, text)
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
    elif state == "search:query":
        db.clear_session(actor.tg_id)
        run_search(tg, actor, text)
    elif state == "links:youtube":
        handle_add_links_message(tg, actor, data, "youtube", text)
    elif state == "links:tiktok":
        handle_add_links_message(tg, actor, data, "tiktok", text)
    elif state == "links:vk":
        handle_add_links_message(tg, actor, data, "vk", text)
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
    db.set_session(
        tg_id=actor.tg_id,
        chat_id=actor.chat_id,
        username=actor.username,
        state="new:author",
        data=data,
    )
    ask_people(tg, actor, "author")


def ask_people(tg: TelegramClient, actor: Actor, role: str) -> None:
    people = get_people(role)
    short_role = SHORT_BY_ROLE[role]
    rows: list[list[tuple[str, str]]] = []
    if role == "voice":
        rows.append([("Нет", "vn")])
    for index in range(0, len(people), 2):
        row: list[tuple[str, str]] = []
        for person in people[index : index + 2]:
            row.append((person["name"], f"p:{short_role}:{person['id']}"))
        rows.append(row)
    rows.append([("Нет в списке", f"pm:{short_role}")])
    label = {
        "author": "Выберите автора.",
        "voice": "Нужна дополнительная озвучка?",
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
        "SELECT id, name FROM people WHERE id = %s AND role = %s AND is_active = true",
        (person_id, role),
    )
    if not person:
        tg.send_message(actor.chat_id, "Этого человека нет в активном списке.")
        return

    data = session.get("data") or {}
    data[f"{role}_id"] = person["id"]
    data[f"{role}_name"] = person["name"]
    next_after_person(tg, actor, role, data)


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
    next_after_person(tg, actor, role, data)


def handle_voice_none(tg: TelegramClient, actor: Actor) -> None:
    session = db.get_session(actor.tg_id)
    if not session:
        tg.send_message(actor.chat_id, "Начните заявку заново: /new_video.")
        return
    data = session.get("data") or {}
    data["voice_id"] = None
    data["voice_name"] = None
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
    keep = {
        "edit_video_id": data.get("edit_video_id"),
        "instagram_url": data.get("instagram_url"),
        "instagram_id": data.get("instagram_id"),
    }
    db.set_session(
        tg_id=actor.tg_id,
        chat_id=actor.chat_id,
        username=actor.username,
        state="new:author",
        data=keep,
    )
    tg.send_message(actor.chat_id, "Ок, оставляю Instagram и пройдём поля заново.")
    ask_people(tg, actor, "author")


def submit_video(tg: TelegramClient, actor: Actor) -> None:
    session = db.get_session(actor.tg_id)
    if not session:
        tg.send_message(actor.chat_id, "Начните заявку заново: /new_video.")
        return
    data = session.get("data") or {}
    if not data.get("instagram_id"):
        tg.send_message(actor.chat_id, "В заявке нет Instagram ID. Начните заново: /new_video.")
        return

    edit_video_id = int(data["edit_video_id"]) if data.get("edit_video_id") else None
    duplicate = find_video_by_instagram_id(data["instagram_id"])
    if duplicate and duplicate.get("id") != edit_video_id:
        db.clear_session(actor.tg_id)
        tg.send_message(actor.chat_id, format_video_card(duplicate, title="Такое видео уже есть"))
        return

    try:
        if edit_video_id:
            video = update_revision_video(actor, edit_video_id, data)
        else:
            video = insert_pending_video(actor, data)
    except psycopg.errors.UniqueViolation:
        duplicate = find_video_by_instagram_id(data["instagram_id"])
        db.clear_session(actor.tg_id)
        if duplicate:
            tg.send_message(actor.chat_id, format_video_card(duplicate, title="Такое видео уже есть"))
        else:
            tg.send_message(actor.chat_id, "Похоже, заявка уже была добавлена. Проверьте /my_requests.")
        return
    except RuntimeError as exc:
        db.clear_session(actor.tg_id)
        tg.send_message(actor.chat_id, _safe_error(exc))
        return

    db.clear_session(actor.tg_id)
    tg.send_message(actor.chat_id, "Заявка отправлена на проверку.")
    notify_admin_queue(tg, video)


def update_revision_video(actor: Actor, video_id: int, data: dict[str, Any]) -> dict[str, Any]:
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
                    montage_id = %s,
                    montage_name = %s,
                    voice_id = %s,
                    voice_name = %s,
                    checked_by_tg_id = NULL,
                    checked_by_username = NULL,
                    checked_at = NULL,
                    batch_id = %s,
                    updated_at = now()
                WHERE id = %s AND status = 'needs_revision'
                RETURNING id
                """,
                (
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
                    data.get("montage_id"),
                    data.get("montage_name"),
                    data.get("voice_id"),
                    data.get("voice_name"),
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
            after_data={"status": "pending", "batch_id": batch_id},
        )
        return get_video_by_id(conn, video_id)


def insert_pending_video(actor: Actor, data: dict[str, Any]) -> dict[str, Any]:
    with db.transaction() as conn:
        batch_id = ensure_open_batch(conn, actor)
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO videos (
                    status, publish_date, instagram_url, instagram_id,
                    youtube_url, youtube_id, tiktok_url, tiktok_id, vk_url, vk_id,
                    author_id, author_name, montage_id, montage_name, voice_id, voice_name,
                    added_by_tg_id, added_by_username, batch_id
                )
                VALUES (
                    'pending', %s, %s, %s,
                    %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s,
                    %s, %s, %s
                )
                RETURNING id
                """,
                (
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
                    data.get("montage_id"),
                    data.get("montage_name"),
                    data.get("voice_id"),
                    data.get("voice_name"),
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
            after_data={"batch_id": batch_id},
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
                      AND instagram_id IS NOT NULL
                      AND COALESCE(author_name, '') <> ''
                      AND COALESCE(montage_name, '') <> ''
                ) AS clean_count,
                count(*) FILTER (WHERE status = 'duplicate') AS duplicate_count,
                count(*) FILTER (
                    WHERE status = 'pending'
                      AND (
                        publish_date IS NULL
                        OR instagram_id IS NULL
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


def notify_admin_queue(tg: TelegramClient, video: dict[str, Any]) -> None:
    settings = get_settings()
    try:
        batch = db.fetch_one("SELECT * FROM batches WHERE id = %s", (video["batch_id"],))
        if not batch:
            return
        if int(batch.get("total_count") or 0) <= 1:
            tg.send_message(
                settings.admin_chat_id,
                format_video_card(video, title="Новая заявка"),
                admin_video_keyboard(video["id"], video["batch_id"], 0),
            )
        else:
            send_batch_summary(tg, settings.admin_chat_id, int(video["batch_id"]))
    except Exception as exc:
        record_system_log(
            "admin_notify_failed",
            "video",
            int(video["id"]),
            {"error": _safe_error(exc)},
        )


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
    data = {
        "edit_video_id": video_id,
        "instagram_url": video.get("instagram_url"),
        "instagram_id": video.get("instagram_id"),
    }
    db.set_session(
        tg_id=actor.tg_id,
        chat_id=actor.chat_id,
        username=actor.username,
        state="new:author",
        data=data,
    )
    tg.send_message(actor.chat_id, "Ок, исправим заявку и вернём её в очередь.")
    ask_people(tg, actor, "author")


def show_admin(tg: TelegramClient, actor: Actor) -> None:
    if not require_admin(tg, actor):
        return
    with db.transaction() as conn:
        assign_orphan_pending(conn, actor)
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT b.*
                FROM batches b
                WHERE b.status = 'open'
                  AND EXISTS (
                    SELECT 1 FROM videos v
                    WHERE v.batch_id = b.id AND v.status = 'pending'
                  )
                ORDER BY b.updated_at DESC
                LIMIT 10
                """
            )
            batches = list(cur.fetchall())
    if not batches:
        tg.send_message(actor.chat_id, "В очереди нет заявок.")
        return
    for batch in batches:
        send_batch_summary(tg, actor.chat_id, int(batch["id"]))


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
    try:
        publish_date = parse_publish_date(text)
    except ValueError as exc:
        tg.send_message(actor.chat_id, str(exc))
        return
    db.clear_session(actor.tg_id)
    set_video_publish_date(
        tg,
        actor,
        int(data["video_id"]),
        int(data["batch_id"]),
        int(data["index"]),
        publish_date.isoformat(),
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
    sync_video_after_approval(video, actor)
    send_final_card(video, actor)
    tg.send_message(actor.chat_id, f"Заявка #{video_id} одобрена.")
    show_queue_item(tg, actor, batch_id, index, edit_message_id)


def approve_video_in_db(video_id: int, actor: Actor) -> dict[str, Any] | None:
    with db.transaction() as conn:
        before = get_video_by_id(conn, video_id)
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE videos
                SET status = 'approved',
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


def sync_video_after_approval(video: dict[str, Any], actor: Actor) -> None:
    try:
        row_number = sheets.upsert_video(video)
        if row_number:
            db.execute("UPDATE videos SET sheet_row = %s, updated_at = now() WHERE id = %s", (row_number, video["id"]))
            video["sheet_row"] = row_number
        record_system_log(
            "sync_sheets_ok",
            "video",
            int(video["id"]),
            {"sheet_row": row_number},
            actor,
        )
    except Exception as exc:
        record_system_log(
            "sync_sheets_failed",
            "video",
            int(video["id"]),
            {"error": _safe_error(exc)},
            actor,
        )


def send_final_card(video: dict[str, Any], actor: Actor) -> None:
    try:
        TelegramClient().send_message(get_settings().work_chat_id, format_final_card(video))
    except Exception as exc:
        record_system_log(
            "work_chat_notify_failed",
            "video",
            int(video["id"]),
            {"error": _safe_error(exc)},
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
          AND instagram_id IS NOT NULL
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
        sync_video_after_approval(video, actor)
        send_final_card(video, actor)
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
    lines = ["Сводка:"]
    for row in rows:
        lines.append(f"{row['status']}: {row['count']}")
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


def start_or_run_search(tg: TelegramClient, actor: Actor, query: str) -> None:
    if not require_admin(tg, actor):
        return
    if query:
        run_search(tg, actor, query)
        return
    db.set_session(
        tg_id=actor.tg_id,
        chat_id=actor.chat_id,
        username=actor.username,
        state="search:query",
        data={},
    )
    tg.send_message(actor.chat_id, "Введите ID, shortcode, ссылку или имя.")


def run_search(tg: TelegramClient, actor: Actor, query: str) -> None:
    q = query.strip()
    instagram_id = None
    try:
        instagram_id = normalize_instagram(q).external_id
    except Exception:
        pass
    params: list[Any] = []
    clauses = []
    if q.isdigit():
        clauses.append("v.id = %s")
        params.append(int(q))
    if instagram_id:
        clauses.append("v.instagram_id = %s")
        params.append(instagram_id)
    clauses.append(
        """
        (
            v.instagram_id ILIKE %s OR v.author_name ILIKE %s OR
            v.montage_name ILIKE %s OR v.voice_name ILIKE %s OR
            v.youtube_url ILIKE %s OR v.tiktok_url ILIKE %s OR v.vk_url ILIKE %s
        )
        """
    )
    like = f"%{q}%"
    params.extend([like, like, like, like, like, like, like])
    rows = db.fetch_all(
        VIDEO_SELECT
        + " WHERE "
        + " OR ".join(clauses)
        + " ORDER BY v.created_at DESC LIMIT 10",
        tuple(params),
    )
    if not rows:
        tg.send_message(actor.chat_id, "Ничего не найдено.")
        return
    for row in rows:
        tg.send_message(actor.chat_id, format_video_card(row, title="Найдено"))


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
    ok = 0
    failed = 0
    for row in rows:
        try:
            row_number = sheets.upsert_video(row)
            if row_number:
                db.execute("UPDATE videos SET sheet_row = %s, updated_at = now() WHERE id = %s", (row_number, row["id"]))
            ok += 1
        except Exception as exc:
            failed += 1
            record_system_log(
                "sync_sheets_failed",
                "video",
                int(row["id"]),
                {"error": _safe_error(exc)},
                actor,
            )
    tg.send_message(actor.chat_id, f"Синхронизация завершена. Успешно: {ok}, ошибок: {failed}.")


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
        sync_video_after_approval(after, actor)
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
        sync_video_after_approval(after, actor)
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
