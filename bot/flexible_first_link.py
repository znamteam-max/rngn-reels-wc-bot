from __future__ import annotations

from typing import Any

from bot import cancel_command, db
from bot import handlers as h
from bot.links import normalize_instagram, normalize_optional
from bot.telegram import TelegramClient, inline_keyboard


FIRST_LINK_SESSION = "new:first_link"
AIR_CUT_FIRST_LINK_SESSION = "new:aircut_first_link"
REMAINING_LINK_PREFIX = "new:remaining:"
PLATFORMS = ("instagram", "youtube", "tiktok", "vk")
PLATFORM_LABELS = {
    "instagram": "Instagram/Reels",
    "youtube": "YouTube/Shorts",
    "tiktok": "TikTok",
    "vk": "VK",
}
VM_PROJECT_CODE = "vzyal_myach"
AIR_CUT_MARKER = "Отрез из эфира"
_INSTALLED = False


def _parse_platform_link(platform: str, text: str):
    if platform == "instagram":
        return normalize_instagram(text)
    return normalize_optional(platform, text)


def parse_first_link(text: str) -> tuple[str, Any]:
    for platform in PLATFORMS:
        try:
            link = _parse_platform_link(platform, text)
        except ValueError:
            continue
        if link and getattr(link, "url", None):
            return platform, link
    raise ValueError("Нужна ссылка Instagram, YouTube, TikTok или VK.")


def _duplicate_for(platform: str, link: Any) -> dict[str, Any] | None:
    external_id = str(getattr(link, "external_id", None) or "").strip()
    url = str(getattr(link, "url", None) or "").strip()
    clauses: list[str] = []
    params: list[Any] = []
    if external_id:
        clauses.append(f"v.{platform}_id = %s")
        params.append(external_id)
    if url:
        clauses.append(f"v.{platform}_url = %s")
        params.append(url)
    if not clauses:
        return None
    return db.fetch_one(
        h.VIDEO_SELECT
        + f" WHERE v.status <> 'deleted' AND ({' OR '.join(clauses)}) ORDER BY v.id ASC LIMIT 1",
        tuple(params),
    )


def _store_link(data: dict[str, Any], platform: str, link: Any) -> dict[str, Any]:
    updated = dict(data)
    updated[f"{platform}_url"] = str(link.url)
    updated[f"{platform}_id"] = getattr(link, "external_id", None)
    updated["first_platform"] = updated.get("first_platform") or platform
    return updated


def _next_missing_platform(data: dict[str, Any]) -> str | None:
    skipped = {str(value) for value in data.get("skipped_platforms") or []}
    for platform in PLATFORMS:
        if data.get(f"{platform}_url") or platform in skipped:
            continue
        return platform
    return None


def _ask_next_link(tg: TelegramClient, actor: h.Actor, data: dict[str, Any]) -> None:
    platform = _next_missing_platform(data)
    if platform is None:
        h.ask_submission_date(tg, actor, data)
        return
    db.set_session(
        tg_id=actor.tg_id,
        chat_id=actor.chat_id,
        username=actor.username,
        state=f"{REMAINING_LINK_PREFIX}{platform}",
        data=data,
    )
    tg.send_message(
        actor.chat_id,
        f"Пришлите ссылку {PLATFORM_LABELS[platform]} на эту же работу или пропустите.",
        inline_keyboard([[('Пропустить', f'flexskip:{platform}')]]),
    )


def _handle_first_link(
    tg: TelegramClient,
    actor: h.Actor,
    data: dict[str, Any],
    text: str,
    *,
    aircut: bool,
) -> None:
    try:
        platform, link = parse_first_link(text)
    except ValueError as exc:
        tg.send_message(actor.chat_id, str(exc))
        return

    duplicate = _duplicate_for(platform, link)
    if duplicate:
        db.clear_session(actor.tg_id)
        tg.send_message(actor.chat_id, h.format_video_card(duplicate, title="Такое видео уже есть"))
        return

    data = _store_link(data, platform, link)
    if aircut:
        db.set_session(
            tg_id=actor.tg_id,
            chat_id=actor.chat_id,
            username=actor.username,
            state="new:author",
            data=data,
        )
        h.ask_people(tg, actor, "author")
        return
    h.ask_submission_project(tg, actor, data)


def _handle_remaining_link(
    tg: TelegramClient,
    actor: h.Actor,
    data: dict[str, Any],
    platform: str,
    text: str,
) -> None:
    try:
        link = _parse_platform_link(platform, text)
    except ValueError:
        tg.send_message(
            actor.chat_id,
            f"Это не похоже на ссылку {PLATFORM_LABELS[platform]}. Пришлите корректную ссылку или нажмите «Пропустить».",
        )
        return
    if not link:
        tg.send_message(actor.chat_id, "Пришлите ссылку или нажмите «Пропустить».")
        return
    data = _store_link(data, platform, link)
    _ask_next_link(tg, actor, data)


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    original_start_new_video = h.start_new_video
    original_next_after_person = h.next_after_person
    original_handle_session_message = h.handle_session_message
    original_handle_message = h.handle_message
    original_handle_callback = h.handle_callback

    def start_new_video(tg: TelegramClient, actor: h.Actor) -> None:
        if actor.chat_type != "private":
            original_start_new_video(tg, actor)
            return
        db.set_session(
            tg_id=actor.tg_id,
            chat_id=actor.chat_id,
            username=actor.username,
            state=FIRST_LINK_SESSION,
            data={
                "video_type": h.VIDEO_TYPE_REGULAR,
                "platform_flow": "any_first",
            },
        )
        tg.send_message(
            actor.chat_id,
            "Пришлите первую ссылку на ролик: Instagram, YouTube, TikTok или VK.",
        )

    def start_aircut(tg: TelegramClient, actor: h.Actor) -> None:
        if actor.chat_type != "private":
            username = h.get_settings().bot_username or "rngn_reels_wc_bot"
            tg.send_message(
                actor.chat_id,
                f"Отрез из эфира нужно добавлять в личке с ботом. Открой @{username}.",
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
            state=AIR_CUT_FIRST_LINK_SESSION,
            data={
                "video_type": h.VIDEO_TYPE_REGULAR,
                "platform_flow": "any_first",
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
            "Пришлите первую ссылку отреза из эфира: Instagram, YouTube, TikTok или VK.",
        )

    def next_after_person(
        tg: TelegramClient,
        actor: h.Actor,
        role: str,
        data: dict[str, Any],
    ) -> None:
        if (
            role == "montage"
            and bool(data.get("date_after_links"))
            and h.normalize_video_type(data.get("video_type")) == h.VIDEO_TYPE_REGULAR
        ):
            _ask_next_link(tg, actor, data)
            return
        original_next_after_person(tg, actor, role, data)

    def handle_session_message(
        tg: TelegramClient,
        actor: h.Actor,
        state: str,
        data: dict[str, Any],
        text: str,
    ) -> None:
        if state == FIRST_LINK_SESSION:
            _handle_first_link(tg, actor, data, text, aircut=False)
            return
        if state == AIR_CUT_FIRST_LINK_SESSION:
            _handle_first_link(tg, actor, data, text, aircut=True)
            return
        if state.startswith(REMAINING_LINK_PREFIX):
            platform = state[len(REMAINING_LINK_PREFIX) :]
            if platform not in PLATFORMS:
                db.clear_session(actor.tg_id)
                tg.send_message(actor.chat_id, "Состояние формы устарело. Начните заново: /new_video.")
                return
            _handle_remaining_link(tg, actor, data, platform, text)
            return
        original_handle_session_message(tg, actor, state, data, text)

    def handle_message(message: dict[str, Any]) -> None:
        if cancel_command.handle_message(message):
            return
        actor = h._actor_from_message(message)
        text = str(message.get("text") or "").strip()
        if actor and text.startswith("/"):
            command, _ = h._command_parts(text)
            if command == "/new_video":
                start_new_video(TelegramClient(), actor)
                return
            if command in {"/new_aircut", "/aircut"}:
                start_aircut(TelegramClient(), actor)
                return
        original_handle_message(message)

    def handle_callback(callback: dict[str, Any]) -> None:
        actor = h._actor_from_callback(callback)
        data = str(callback.get("data") or "")
        if actor and data == "cmd:new":
            tg = TelegramClient()
            try:
                tg.answer_callback_query(callback["id"])
            except Exception:
                pass
            start_new_video(tg, actor)
            return
        if actor and data == "cmd:new_aircut":
            tg = TelegramClient()
            try:
                tg.answer_callback_query(callback["id"])
            except Exception:
                pass
            start_aircut(tg, actor)
            return
        if actor and data.startswith("flexskip:"):
            platform = data.split(":", 1)[1]
            session = db.get_session(actor.tg_id)
            if not session or session.get("state") != f"{REMAINING_LINK_PREFIX}{platform}":
                TelegramClient().send_message(actor.chat_id, "Кнопка устарела. Начните заново: /new_video.")
                return
            tg = TelegramClient()
            try:
                tg.answer_callback_query(callback["id"])
            except Exception:
                pass
            session_data = dict(session.get("data") or {})
            skipped = [str(value) for value in session_data.get("skipped_platforms") or []]
            if platform not in skipped:
                skipped.append(platform)
            session_data["skipped_platforms"] = skipped
            _ask_next_link(tg, actor, session_data)
            return
        original_handle_callback(callback)

    h.start_new_video = start_new_video
    h.next_after_person = next_after_person
    h.handle_session_message = handle_session_message
    h.handle_message = handle_message
    h.handle_callback = handle_callback

    _INSTALLED = True
