from __future__ import annotations

from typing import Any

from bot import db
from bot import handlers as h
from bot import sheets
from bot.links import normalize_instagram, normalize_optional
from bot.messages import person_display
from bot.telegram import TelegramClient, inline_keyboard


VM_PROJECT_CODE = "vzyal_myach"
WORLD_CUP_PROJECT_CODE = "world_cup_2026"
AIR_CUT_MARKER = "Отрез из эфира"
AIR_CUT_SESSION = "new:aircut_instagram"

VM_AUTHOR_ROSTER: tuple[dict[str, Any], ...] = (
    {
        "display_name": "Артём Тихонов",
        "display_username": "tikhonov32",
        "lookup_names": ("Артём Тихонов", "Тихонов"),
        "lookup_username": "tikhonov32",
        "sort_weight": 100,
    },
    {
        "display_name": "Знамбо",
        "display_username": "ZnamBo",
        "lookup_names": ("Знамбо",),
        "lookup_username": "znambo",
        "sort_weight": 90,
    },
    {
        "display_name": "Матвей Юдкин",
        "display_username": None,
        "lookup_names": ("Матвей Юдкин", "Юдкин"),
        "lookup_username": "yudkiin",
        "sort_weight": 80,
    },
    {
        "display_name": "Сергей Абаев",
        "display_username": "SergeAbaka",
        "lookup_names": ("Сергей Абаев", "Абаев"),
        "lookup_username": "SergeAbaka",
        "sort_weight": 70,
    },
)

_INSTALLED = False


def _project_code_from_session(actor: h.Actor) -> str:
    session = db.get_session(actor.tg_id)
    data = session.get("data") if session else {}
    return str((data or {}).get("project_code") or "")


def _find_or_create_vm_author(spec: dict[str, Any]) -> dict[str, Any]:
    username = str(spec.get("lookup_username") or "").strip()
    names = tuple(
        str(name).strip()
        for name in spec.get("lookup_names") or ()
        if str(name).strip()
    )
    clauses: list[str] = []
    params: list[Any] = ["author"]
    if username:
        clauses.append("lower(COALESCE(username, '')) = lower(%s)")
        params.append(username)
    if names:
        clauses.append("lower(name) = ANY(%s)")
        params.append([name.lower() for name in names])
    if not clauses:
        raise ValueError("project author lookup requires username or name")

    with db.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT id, name, username, role, is_active, sort_weight
                FROM people
                WHERE role = %s
                  AND ({' OR '.join(clauses)})
                ORDER BY
                    CASE WHEN lower(COALESCE(username, '')) = lower(%s) THEN 0 ELSE 1 END,
                    CASE WHEN is_active THEN 0 ELSE 1 END,
                    sort_weight DESC,
                    id ASC
                LIMIT 1
                """,
                (*params, username),
            )
            existing = cur.fetchone()
            if existing:
                if not bool(existing.get("is_active")):
                    cur.execute(
                        """
                        UPDATE people
                        SET is_active = true
                        WHERE id = %s
                        RETURNING id, name, username, role, is_active, sort_weight
                        """,
                        (int(existing["id"]),),
                    )
                    return cur.fetchone()
                return existing

            display_name = str(spec["display_name"])
            insert_username = (
                str(spec.get("display_username") or username or "").strip() or None
            )
            cur.execute(
                """
                INSERT INTO people (name, username, role, is_active, sort_weight)
                VALUES (%s, %s, 'author', true, %s)
                RETURNING id, name, username, role, is_active, sort_weight
                """,
                (display_name, insert_username, int(spec.get("sort_weight") or 0)),
            )
            row = cur.fetchone()
            if not row:
                raise RuntimeError(f"failed to create project author {display_name}")
            return row


def _vm_authors() -> list[tuple[dict[str, Any], dict[str, Any]]]:
    return [(spec, _find_or_create_vm_author(spec)) for spec in VM_AUTHOR_ROSTER]


def _author_rows(
    people: list[tuple[dict[str, Any] | None, dict[str, Any]]],
) -> list[list[tuple[str, str]]]:
    buttons: list[tuple[str, str]] = []
    for spec, person in people:
        name = str((spec or {}).get("display_name") or person.get("name") or "")
        username = (spec or {}).get("display_username")
        if username is None:
            username = person.get("username")
        buttons.append(
            (
                person_display(name, str(username) if username else None),
                f"p:a:{int(person['id'])}",
            )
        )
    return [buttons[index : index + 2] for index in range(0, len(buttons), 2)]


def _is_aircut_data(data: dict[str, Any]) -> bool:
    return (
        str(data.get("submission_kind") or "") == "aircut"
        or str(data.get("comment") or "").strip() == AIR_CUT_MARKER
    )


def _is_aircut_video(video: dict[str, Any]) -> bool:
    return str(video.get("comment") or "").strip() == AIR_CUT_MARKER


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    original_ask_people = h.ask_people
    original_continue_after_project = h._continue_after_project
    original_handle_optional_link = h.handle_optional_link
    original_handle_new_date = h.handle_new_date
    original_handle_message = h.handle_message
    original_handle_callback = h.handle_callback
    original_handle_session_message = h.handle_session_message
    original_insert_pending_video = h.insert_pending_video
    original_update_revision_video = h.update_revision_video
    original_handle_preview_edit = h.handle_preview_edit
    original_video_to_row = sheets.video_to_row

    def send_main_menu(tg: TelegramClient, actor: h.Actor, text: str) -> None:
        rows = [
            [("➕ Добавить ролик", "cmd:new")],
            [("✂️ Отрез из эфира · Взял Мяч", "cmd:new_aircut")],
            [("🧵 Добавить большой рекап", "cmd:new_bigrecap")],
            [("📋 Мои заявки", "cmd:my"), ("ℹ️ Помощь", "cmd:help")],
        ]
        if h.is_superadmin(actor.tg_id):
            rows.append([("⚡ Добавить мой ролик", "cmd:add_znambo")])
        if h.is_admin(actor.tg_id):
            rows.insert(4, [("Админка", "cmd:admin"), ("Сводка", "cmd:summary")])
            rows.insert(5, [("👥 Сверка работ", "ar:start")])
            rows.insert(
                6,
                [
                    ("Статус очереди", "cmd:queue_status"),
                    ("Восстановить очередь", "cmd:resend_pending"),
                ],
            )
            rows.insert(7, [("Тест админ-чата", "cmd:test_admin_chat")])
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

        project_code = _project_code_from_session(actor)
        if project_code == VM_PROJECT_CODE:
            rows = _author_rows(_vm_authors())
            tg.send_message(
                actor.chat_id,
                "Выберите автора · Взял Мяч.",
                inline_keyboard(rows),
            )
            return

        if project_code:
            people = [
                person
                for person in h.get_people("author")
                if str(person.get("username") or "").casefold() != "sergeabaka"
            ]
            rows = _author_rows([(None, person) for person in people])
            rows.append([("Нет в списке", "pm:a")])
            label = (
                "Выберите автора · ЧМ 2026."
                if project_code == WORLD_CUP_PROJECT_CODE
                else "Выберите автора."
            )
            tg.send_message(actor.chat_id, label, inline_keyboard(rows))
            return

        original_ask_people(tg, actor, role, show_voice_decision)

    def continue_after_project(
        tg: TelegramClient,
        actor: h.Actor,
        data: dict[str, Any],
        *,
        znambo_flow: bool,
    ) -> None:
        if (
            znambo_flow
            or h.normalize_video_type(data.get("video_type"))
            == h.VIDEO_TYPE_BIGRECAP
        ):
            original_continue_after_project(
                tg,
                actor,
                data,
                znambo_flow=znambo_flow,
            )
            return
        data["date_after_links"] = True
        db.set_session(
            tg_id=actor.tg_id,
            chat_id=actor.chat_id,
            username=actor.username,
            state="new:author",
            data=data,
        )
        h.ask_people(tg, actor, "author")

    def handle_optional_link(
        tg: TelegramClient,
        actor: h.Actor,
        platform: str,
        text: str,
    ) -> None:
        session = db.get_session(actor.tg_id)
        data = session.get("data") if session else {}
        if platform != "vk" or not bool((data or {}).get("date_after_links")):
            original_handle_optional_link(tg, actor, platform, text)
            return
        try:
            link = normalize_optional(platform, text)
        except ValueError:
            tg.send_message(
                actor.chat_id,
                "Не удалось разобрать ссылку. Пришлите её ещё раз или нажмите «Пропустить».",
            )
            return
        data = dict(data or {})
        if link:
            data["vk_url"] = link.url
            data["vk_id"] = link.external_id
        h.ask_submission_date(tg, actor, data)

    def handle_new_date(tg: TelegramClient, actor: h.Actor, text: str) -> None:
        session = db.get_session(actor.tg_id)
        data = session.get("data") if session else {}
        if not bool((data or {}).get("date_after_links")) or not (data or {}).get(
            "author_name"
        ):
            original_handle_new_date(tg, actor, text)
            return
        if not session or session.get("state") not in {
            h.NEW_DATE_SESSION,
            h.NEW_DATE_MANUAL_SESSION,
        }:
            tg.send_message(actor.chat_id, "Начните заявку заново: /new_video.")
            return
        try:
            publish_date = h.parse_new_submission_date(text)
        except ValueError:
            tg.send_message(actor.chat_id, h.NEW_DATE_INVALID_MESSAGE)
            return
        data = dict(data or {})
        data["publish_date"] = publish_date.isoformat()
        h.show_new_preview(tg, actor, data)

    def start_aircut(tg: TelegramClient, actor: h.Actor) -> None:
        if actor.chat_type != "private":
            username = h.get_settings().bot_username or "rngn_reels_wc_bot"
            tg.send_message(
                actor.chat_id,
                "Отрез из эфира нужно добавлять в личке с ботом. "
                f"Открой @{username} и выбери «Отрез из эфира».",
            )
            return
        project = h.get_active_project(VM_PROJECT_CODE)
        if not project:
            tg.send_message(actor.chat_id, "Проект «Взял Мяч» сейчас недоступен.")
            return
        db.set_session(
            tg_id=actor.tg_id,
            chat_id=actor.chat_id,
            username=actor.username,
            state=AIR_CUT_SESSION,
            data={
                "video_type": h.VIDEO_TYPE_REGULAR,
                "platform_flow": h.PLATFORM_FLOW_REGULAR,
                "submission_kind": "aircut",
                "comment": AIR_CUT_MARKER,
                "date_after_links": True,
                "project_id": int(project["id"]),
                "project_code": str(project["code"]),
                "project_name": str(project["name"]),
            },
        )
        tg.send_message(
            actor.chat_id,
            "Пришлите Instagram/Reels ссылку отреза из эфира.",
        )

    def handle_aircut_instagram(
        tg: TelegramClient,
        actor: h.Actor,
        data: dict[str, Any],
        text: str,
    ) -> None:
        try:
            link = normalize_instagram(text)
        except ValueError as exc:
            tg.send_message(actor.chat_id, str(exc))
            return
        duplicate = h.find_video_by_instagram_id(link.external_id or "")
        if duplicate:
            db.clear_session(actor.tg_id)
            tg.send_message(
                actor.chat_id,
                h.format_video_card(duplicate, title="Такое видео уже есть"),
            )
            return
        data = dict(data)
        data.update({"instagram_url": link.url, "instagram_id": link.external_id})
        db.set_session(
            tg_id=actor.tg_id,
            chat_id=actor.chat_id,
            username=actor.username,
            state="new:author",
            data=data,
        )
        h.ask_people(tg, actor, "author")

    def handle_session_message(
        tg: TelegramClient,
        actor: h.Actor,
        state: str,
        data: dict[str, Any],
        text: str,
    ) -> None:
        if state == AIR_CUT_SESSION:
            handle_aircut_instagram(tg, actor, data, text)
            return
        original_handle_session_message(tg, actor, state, data, text)

    def handle_message(message: dict[str, Any]) -> None:
        actor = h._actor_from_message(message)
        text = str(message.get("text") or "").strip()
        if actor and text.startswith("/"):
            command, _ = h._command_parts(text)
            if command in {"/new_aircut", "/aircut"}:
                start_aircut(TelegramClient(), actor)
                return
        original_handle_message(message)

    def handle_callback(callback: dict[str, Any]) -> None:
        actor = h._actor_from_callback(callback)
        data = str(callback.get("data") or "")
        if actor and data == "cmd:new_aircut":
            tg = TelegramClient()
            try:
                tg.answer_callback_query(callback["id"])
            except Exception:
                pass
            start_aircut(tg, actor)
            return
        original_handle_callback(callback)

    def insert_pending_video(actor: h.Actor, data: dict[str, Any]) -> dict[str, Any]:
        video = original_insert_pending_video(actor, data)
        if _is_aircut_data(data) and video and video.get("id"):
            db.execute(
                "UPDATE videos SET comment = %s, updated_at = now() WHERE id = %s",
                (AIR_CUT_MARKER, int(video["id"])),
            )
            refreshed = h.get_video_by_id_outside(int(video["id"]))
            return refreshed or video
        return video

    def update_revision_video(
        actor: h.Actor,
        video_id: int,
        data: dict[str, Any],
    ) -> dict[str, Any]:
        video = original_update_revision_video(actor, video_id, data)
        if _is_aircut_data(data):
            db.execute(
                "UPDATE videos SET comment = %s, updated_at = now() WHERE id = %s",
                (AIR_CUT_MARKER, int(video_id)),
            )
            refreshed = h.get_video_by_id_outside(int(video_id))
            return refreshed or video
        return video

    def handle_preview_edit(tg: TelegramClient, actor: h.Actor) -> None:
        session = db.get_session(actor.tg_id)
        data = session.get("data") if session else {}
        was_aircut = _is_aircut_data(data or {})
        original_handle_preview_edit(tg, actor)
        if not was_aircut:
            return
        updated = db.get_session(actor.tg_id)
        if not updated:
            return
        updated_data = dict(updated.get("data") or {})
        updated_data.update(
            {
                "submission_kind": "aircut",
                "comment": AIR_CUT_MARKER,
                "date_after_links": True,
            }
        )
        db.set_session(
            tg_id=actor.tg_id,
            chat_id=actor.chat_id,
            username=actor.username,
            state=str(updated.get("state") or "new:author"),
            data=updated_data,
        )

    def video_to_row(
        video: dict[str, Any],
        columns: list[str] | None = None,
    ) -> list[str]:
        selected_columns = columns or sheets.SHEET_COLUMNS
        row = original_video_to_row(video, selected_columns)
        if _is_aircut_video(video) and "video_type" in selected_columns:
            row[selected_columns.index("video_type")] = "aircut"
        return row

    h._send_main_menu = send_main_menu
    h.ask_people = ask_people
    h._continue_after_project = continue_after_project
    h.handle_optional_link = handle_optional_link
    h.handle_new_date = handle_new_date
    h.handle_session_message = handle_session_message
    h.handle_message = handle_message
    h.handle_callback = handle_callback
    h.insert_pending_video = insert_pending_video
    h.update_revision_video = update_revision_video
    h.handle_preview_edit = handle_preview_edit
    sheets.video_to_row = video_to_row

    _INSTALLED = True
