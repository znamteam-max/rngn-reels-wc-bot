from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from typing import Any

from bot import admin_queue, db, reconciliation, sheet_layout, sheets
from bot.config import get_settings
from bot.messages import format_video_card
from bot.telegram import TelegramClient

AUTHOR_SUBMISSIONS_SHEET = "Заявки по авторам"
PAYMENTS_SHEET = "Выплаты"
AUTHOR_SUBMISSION_COLUMNS = [
    "Месяц подачи",
    "Автор",
    "Username",
    "Подано заявок",
    "Обработано",
    "Одобрено",
    "Ждёт проверки",
    "На доработке",
    "Дубликаты",
    "Обычные Reels",
    "Big Recap",
]
PAYMENT_COLUMNS = [
    "ID",
    "Автор",
    "Тип",
    "Проект",
    "Дата публикации",
    "Месяц публикации",
    "Заявка подана",
    "Месяц выплаты",
    "Опоздал к дедлайну",
    "Выплачено",
    "Дата выплаты",
    "Кто отметил",
    "Instagram",
    "YouTube",
]
VIDEO_PAYMENT_COLUMNS = [
    "Месяц выплаты",
    "Опоздал к дедлайну",
    "Выплачено",
    "Дата выплаты",
]


def ensure_payment_schema() -> None:
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS video_payments (
            video_id bigint PRIMARY KEY REFERENCES videos(id) ON DELETE CASCADE,
            is_paid boolean NOT NULL DEFAULT false,
            paid_at timestamptz NULL,
            paid_by_tg_id bigint NULL,
            paid_by_username text NULL,
            note text NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )


def _as_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _month_start(value: date) -> date:
    return value.replace(day=1)


def _next_month(value: date) -> date:
    if value.month == 12:
        return date(value.year + 1, 1, 1)
    return date(value.year, value.month + 1, 1)


def _local_submission_date(video: dict[str, Any]) -> date | None:
    value = video.get("created_at")
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(get_settings().tz)
        return value.date()
    return _as_date(value)


def payout_schedule(video: dict[str, Any]) -> tuple[str, bool]:
    publish_date = _as_date(video.get("publish_date"))
    submitted = _local_submission_date(video)
    if publish_date is None or submitted is None:
        return "", False

    base_cycle = _next_month(_month_start(publish_date))
    submitted_month = _month_start(submitted)
    submission_cycle = submitted_month if submitted.day <= 15 else _next_month(submitted_month)
    payout = max(base_cycle, submission_cycle)
    return payout.strftime("%Y-%m"), payout > base_cycle


def submission_month(video: dict[str, Any]) -> str:
    submitted = _local_submission_date(video)
    return submitted.strftime("%Y-%m") if submitted else "Без даты"


def _payment_map() -> dict[int, dict[str, Any]]:
    ensure_payment_schema()
    return {
        int(row["video_id"]): row
        for row in db.fetch_all("SELECT * FROM video_payments ORDER BY video_id")
    }


def _active_videos() -> list[dict[str, Any]]:
    from bot import handlers as h

    return db.fetch_all(
        h.VIDEO_SELECT + " WHERE v.status <> 'deleted' ORDER BY v.created_at, v.id"
    )


def _author_key(video: dict[str, Any]) -> tuple[str, str]:
    try:
        person = reconciliation._person_key(video, "author")
    except Exception:
        person = None
    if person:
        return person
    return (
        str(video.get("author_name") or "Не указан"),
        str(video.get("author_username") or "").lstrip("@"),
    )


def build_author_submission_rows(videos: list[dict[str, Any]]) -> list[list[str]]:
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for video in videos:
        if str(video.get("status") or "") == "deleted":
            continue
        name, username = _author_key(video)
        grouped[(submission_month(video), name, username)].append(video)

    rows: list[list[str]] = []
    for (month, name, username), items in sorted(grouped.items()):
        statuses = [str(item.get("status") or "") for item in items]
        rows.append(
            [
                month,
                name,
                f"@{username}" if username else "",
                str(len(items)),
                str(sum(status != "pending" for status in statuses)),
                str(sum(status == "approved" for status in statuses)),
                str(sum(status == "pending" for status in statuses)),
                str(sum(status == "needs_revision" for status in statuses)),
                str(sum(status == "duplicate" for status in statuses)),
                str(sum(str(item.get("video_type") or "regular") != "bigrecap" for item in items)),
                str(sum(str(item.get("video_type") or "regular") == "bigrecap" for item in items)),
            ]
        )
    return rows


def build_payment_rows(
    videos: list[dict[str, Any]],
    payments: dict[int, dict[str, Any]] | None = None,
) -> list[list[str]]:
    payments = payments if payments is not None else _payment_map()
    rows: list[list[str]] = []
    approved = [video for video in videos if video.get("status") == "approved"]
    approved.sort(
        key=lambda video: (
            payout_schedule(video)[0] or "9999-99",
            str(video.get("publish_date") or "9999-99-99"),
            int(video.get("id") or 0),
        )
    )
    for video in approved:
        video_id = int(video["id"])
        payment = payments.get(video_id) or {}
        payout_month, late = payout_schedule(video)
        name, _ = _author_key(video)
        paid_at = payment.get("paid_at")
        if isinstance(paid_at, datetime) and paid_at.tzinfo is not None:
            paid_at = paid_at.astimezone(get_settings().tz)
        paid_by_username = str(payment.get("paid_by_username") or "").lstrip("@")
        paid_by = (
            f"@{paid_by_username}"
            if paid_by_username
            else str(payment.get("paid_by_tg_id") or "")
        )
        rows.append(
            [
                str(video_id),
                name,
                "Big Recap" if video.get("video_type") == "bigrecap" else "Reel",
                str(video.get("project_name") or ""),
                str(video.get("publish_date") or ""),
                reconciliation.publish_month(video) or "",
                str(video.get("created_at") or ""),
                payout_month,
                "Да" if late else "Нет",
                "Выплачено" if payment.get("is_paid") else "Не выплачено",
                paid_at.isoformat(sep=" ", timespec="minutes") if isinstance(paid_at, datetime) else str(paid_at or ""),
                paid_by,
                str(video.get("instagram_url") or ""),
                str(video.get("youtube_url") or ""),
            ]
        )
    return rows


def _video_payment_values(
    video: dict[str, Any],
    payment: dict[str, Any] | None,
) -> list[str]:
    payout_month, late = payout_schedule(video)
    if video.get("status") != "approved":
        paid_label = "Не готово к выплате"
    else:
        paid_label = "Выплачено" if payment and payment.get("is_paid") else "Не выплачено"
    paid_at = (payment or {}).get("paid_at")
    if isinstance(paid_at, datetime) and paid_at.tzinfo is not None:
        paid_at = paid_at.astimezone(get_settings().tz)
    return [
        payout_month,
        "Да" if late else "Нет",
        paid_label,
        paid_at.isoformat(sep=" ", timespec="minutes") if isinstance(paid_at, datetime) else str(paid_at or ""),
    ]


def _append_payment_columns_to_video_sheet(
    service,
    spreadsheet_id: str,
    sheet_name: str,
    videos_by_id: dict[int, dict[str, Any]],
    payments: dict[int, dict[str, Any]],
) -> None:
    table = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=sheets._sheet_range(sheet_name, "A:AZ"))
        .execute()
        .get("values", [])
    )
    if not table:
        return
    header_index = None
    for index, row in enumerate(table[:8]):
        values = [str(value).strip() for value in row]
        if "id" in values and "status" in values:
            header_index = index
            break
    if header_index is None:
        return
    header = [str(value).strip() for value in table[header_index]]
    if "id" not in header:
        return
    id_index = header.index("id")
    for column in VIDEO_PAYMENT_COLUMNS:
        if column not in header:
            header.append(column)
    end_column = sheets._column_letter(len(header))
    values_api = service.spreadsheets().values()
    values_api.update(
        spreadsheetId=spreadsheet_id,
        range=sheets._sheet_range(sheet_name, f"A{header_index + 1}:{end_column}{header_index + 1}"),
        valueInputOption="RAW",
        body={"values": [header]},
    ).execute()

    data_rows = table[header_index + 1 :]
    updates: list[dict[str, Any]] = []
    for payment_column in VIDEO_PAYMENT_COLUMNS:
        column_index = header.index(payment_column)
        column_letter = sheets._column_letter(column_index + 1)
        column_values: list[list[str]] = []
        payment_value_index = VIDEO_PAYMENT_COLUMNS.index(payment_column)
        for row in data_rows:
            raw_id = row[id_index] if len(row) > id_index else ""
            try:
                video_id = int(str(raw_id))
            except (TypeError, ValueError):
                column_values.append([""])
                continue
            video = videos_by_id.get(video_id)
            if not video:
                column_values.append([""])
                continue
            values = _video_payment_values(video, payments.get(video_id))
            column_values.append([values[payment_value_index]])
        if column_values:
            start_row = header_index + 2
            end_row = start_row + len(column_values) - 1
            updates.append(
                {
                    "range": sheets._sheet_range(sheet_name, f"{column_letter}{start_row}:{column_letter}{end_row}"),
                    "values": column_values,
                }
            )
    if updates:
        values_api.batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"valueInputOption": "RAW", "data": updates},
        ).execute()


def _preferred_sheet_order(properties: dict[str, dict[str, Any]]) -> list[str]:
    today = datetime.now(get_settings().tz).date()
    current_month = today.strftime("%Y-%m")
    previous_month = _month_start(today)
    previous_month = date(previous_month.year - 1, 12, 1) if previous_month.month == 1 else date(previous_month.year, previous_month.month - 1, 1)
    previous_month_name = previous_month.strftime("%Y-%m")
    preferred = [
        "Работа авторов",
        AUTHOR_SUBMISSIONS_SHEET,
        PAYMENTS_SHEET,
        "ЧМ 2026",
        "Метрики",
        "Монтаж — справочно",
        current_month,
        previous_month_name,
        "2026-07",
        "2026-06",
        "Videos",
        "Project Stats",
        "Month Stats",
        "Reconciliation",
        "Unfinished Requests",
        "Unsubmitted Forms",
        "Project Backfill Review",
        "Весь Спорт",
        "Без даты",
    ]
    return [name for name in dict.fromkeys(preferred) if name in properties]


def _reorder_reporting_tabs(service, spreadsheet_id: str) -> None:
    properties = sheets._sheet_properties(service, spreadsheet_id)
    preferred = _preferred_sheet_order(properties)
    requests: list[dict[str, Any]] = []
    for title in reversed(preferred):
        requests.append(
            {
                "updateSheetProperties": {
                    "properties": {"sheetId": int(properties[title]["sheetId"]), "index": 0},
                    "fields": "index",
                }
            }
        )
    if requests:
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": requests},
        ).execute()


def sync_reporting_sheets() -> dict[str, Any]:
    ensure_payment_schema()
    settings = get_settings()
    if not settings.google_sheets_spreadsheet_id:
        raise RuntimeError("GOOGLE_SHEETS_SPREADSHEET_ID is not configured")
    videos = _active_videos()
    payments = _payment_map()
    service = sheets._service()
    spreadsheet_id = settings.google_sheets_spreadsheet_id

    sheets._ensure_named_sheets(
        service,
        spreadsheet_id,
        {
            AUTHOR_SUBMISSIONS_SHEET: AUTHOR_SUBMISSION_COLUMNS,
            PAYMENTS_SHEET: PAYMENT_COLUMNS,
        },
    )
    author_rows = build_author_submission_rows(videos)
    payment_rows = build_payment_rows(videos, payments)
    sheets._replace_named_sheet(
        service,
        spreadsheet_id,
        AUTHOR_SUBMISSIONS_SHEET,
        AUTHOR_SUBMISSION_COLUMNS,
        author_rows,
    )
    sheets._replace_named_sheet(
        service,
        spreadsheet_id,
        PAYMENTS_SHEET,
        PAYMENT_COLUMNS,
        payment_rows,
    )

    properties = sheets._sheet_properties(service, spreadsheet_id)
    videos_by_id = {int(video["id"]): video for video in videos}
    month_titles = {
        reconciliation.publish_month(video)
        for video in videos
        if reconciliation.publish_month(video)
    }
    candidate_titles = {
        "Videos",
        "ЧМ 2026",
        "Весь Спорт",
        reconciliation.NO_DATE_SHEET,
        *month_titles,
    }
    for title in sorted(candidate_titles):
        if title in properties:
            _append_payment_columns_to_video_sheet(
                service,
                spreadsheet_id,
                title,
                videos_by_id,
                payments,
            )
    _reorder_reporting_tabs(service, spreadsheet_id)
    return {
        "videos": len(videos),
        "authors_rows": len(author_rows),
        "payment_rows": len(payment_rows),
        "paid": sum(bool(row.get("is_paid")) for row in payments.values()),
        "unpaid_approved": sum(
            video.get("status") == "approved"
            and not bool((payments.get(int(video["id"])) or {}).get("is_paid"))
            for video in videos
        ),
    }


def _actor(message: dict[str, Any]):
    from bot import handlers as h

    return h._actor_from_message(message)


def _require_admin(tg: TelegramClient, actor) -> bool:
    from bot import handlers as h

    return h.require_admin(tg, actor)


def _require_superadmin(tg: TelegramClient, actor) -> bool:
    from bot import handlers as h

    return h.require_superadmin(tg, actor)


def _command_parts(text: str) -> tuple[str, str]:
    from bot import handlers as h

    return h._command_parts(text)


def _safe_error_text(value: Any, limit: int = 160) -> str:
    text = str(value or "-").replace("\n", " ")
    settings = get_settings()
    for secret in (settings.bot_token, settings.database_url, settings.cron_secret):
        if secret:
            text = text.replace(secret, "[secret]")
    return text[:limit]


def show_active_pending(tg: TelegramClient, actor, *, title: str = "Активная заявка") -> None:
    if not _require_admin(tg, actor):
        return
    with db.connect() as conn:
        state = admin_queue.read_queue_state(conn)
        active_id = int(state["active_video_id"]) if state.get("active_video_id") else None
    if not active_id:
        tg.send_message(actor.chat_id, "Активной pending-заявки сейчас нет.")
        return
    from bot import handlers as h

    video = h.get_video_by_id_outside(active_id)
    if not video:
        tg.send_message(actor.chat_id, f"В очереди указан #{active_id}, но запись в videos не найдена.")
        return
    text = format_video_card(video, title=title)
    text += f"\n\nСтатус: {video.get('status')}"
    text += f"\nСоздана: {video.get('created_at')}"
    text += f"\nAdmin message: {state.get('active_message_id') or '—'}"
    tg.send_message(actor.chat_id, text)


def recreate_active_queue_card(tg: TelegramClient, actor) -> None:
    if not _require_superadmin(tg, actor):
        return
    from bot import handlers as h

    with db.transaction() as conn:
        state = admin_queue.queue_state_for_update(conn)
        active_id = int(state["active_video_id"]) if state.get("active_video_id") else None
        old_message_id = state.get("active_message_id")
        admin_queue.clear_active_pointer(conn, reason="manual queue card recreate")
        if active_id:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE videos
                    SET admin_message_chat_id = NULL,
                        admin_message_id = NULL,
                        admin_notified_at = NULL,
                        updated_at = now()
                    WHERE id = %s AND status = 'pending'
                    """,
                    (active_id,),
                )
        db.log_event(
            conn,
            entity_type="admin_queue",
            entity_id=active_id,
            action="queue_card_recreate_requested",
            actor_tg_id=actor.tg_id,
            actor_username=actor.username,
            before_data={"message_id": old_message_id},
        )
    result = h.repair_queue_live_or_enqueue(
        tg,
        actor,
        reason="manual_queue_recreate",
        force=True,
    ) or {}
    tg.send_message(
        actor.chat_id,
        f"Карточка очереди создана заново. Active: #{result.get('active_video_id') or active_id or '—'}.",
    )
    show_active_pending(tg, actor, title="Активная заявка после пересоздания")


def show_video(tg: TelegramClient, actor, raw_id: str) -> None:
    if not _require_admin(tg, actor):
        return
    try:
        video_id = int(raw_id.strip())
    except (TypeError, ValueError):
        tg.send_message(actor.chat_id, "Использование: /video 325")
        return
    from bot import handlers as h

    video = h.get_video_by_id_outside(video_id)
    if not video:
        tg.send_message(actor.chat_id, f"Видео #{video_id} не найдено.")
        return
    text = format_video_card(video, title=f"Видео #{video_id}")
    text += f"\n\nСтатус: {video.get('status')}"
    text += f"\nСоздана: {video.get('created_at')}"
    text += f"\nПроверена: {video.get('checked_at') or '—'}"
    tg.send_message(actor.chat_id, text)


def show_errors(tg: TelegramClient, actor, raw_limit: str) -> None:
    if not _require_admin(tg, actor):
        return
    try:
        limit = max(1, min(20, int(raw_limit or "5")))
    except ValueError:
        limit = 5
    jobs_rows = db.fetch_all(
        """
        SELECT id, kind, status, attempts, last_error, last_failed_at
        FROM background_jobs
        WHERE status IN ('failed','dead')
        ORDER BY COALESCE(last_failed_at, updated_at) DESC, id DESC
        LIMIT %s
        """,
        (limit,),
    )
    update_rows = db.fetch_all(
        """
        SELECT update_id, tg_user_id, chat_id, last_error, finished_at, first_seen_at
        FROM telegram_updates
        WHERE status = 'failed' OR last_error IS NOT NULL
        ORDER BY COALESCE(finished_at, first_seen_at) DESC
        LIMIT %s
        """,
        (limit,),
    )
    log_rows = db.fetch_all(
        """
        SELECT id, action, entity_type, entity_id, created_at
        FROM logs
        WHERE action ILIKE '%%failed%%'
           OR action ILIKE '%%error%%'
        ORDER BY id DESC
        LIMIT %s
        """,
        (limit,),
    )
    lines = ["Последние ошибки", ""]
    if jobs_rows:
        lines.append("Фоновые задания:")
        for row in jobs_rows:
            lines.append(
                f"#{row['id']} {row['kind']} [{row['status']}] попыток={row['attempts']} — {_safe_error_text(row.get('last_error'))}"
            )
    else:
        lines.append("Фоновые задания: ошибок нет.")
    if update_rows:
        lines.append("")
        lines.append("Telegram updates:")
        for row in update_rows:
            lines.append(
                f"update {row['update_id']} user={row.get('tg_user_id') or '—'} — {_safe_error_text(row.get('last_error'))}"
            )
    if log_rows:
        lines.append("")
        lines.append("Системные события:")
        for row in log_rows:
            lines.append(
                f"log#{row['id']} {row['action']} {row.get('entity_type') or ''}#{row.get('entity_id') or ''}"
            )
    tg.send_message(actor.chat_id, "\n".join(lines)[:3900])


def show_logs(tg: TelegramClient, actor, raw_limit: str) -> None:
    if not _require_admin(tg, actor):
        return
    try:
        limit = max(1, min(30, int(raw_limit or "15")))
    except ValueError:
        limit = 15
    rows = db.fetch_all(
        """
        SELECT id, entity_type, entity_id, action, actor_username, actor_tg_id, created_at
        FROM logs
        ORDER BY id DESC
        LIMIT %s
        """,
        (limit,),
    )
    lines = [f"Последние события: {len(rows)}", ""]
    for row in rows:
        created = row.get("created_at")
        if isinstance(created, datetime) and created.tzinfo is not None:
            created = created.astimezone(get_settings().tz)
            time_text = created.strftime("%d.%m %H:%M:%S")
        else:
            time_text = str(created or "—")
        actor_text = f"@{row['actor_username']}" if row.get("actor_username") else str(row.get("actor_tg_id") or "—")
        lines.append(
            f"{time_text} log#{row['id']} {row['action']} · {row.get('entity_type') or '—'}#{row.get('entity_id') or '—'} · {actor_text}"
        )
    tg.send_message(actor.chat_id, "\n".join(lines)[:3900])


def show_failed_jobs(tg: TelegramClient, actor, raw_limit: str) -> None:
    if not _require_admin(tg, actor):
        return
    try:
        limit = max(1, min(30, int(raw_limit or "10")))
    except ValueError:
        limit = 10
    rows = db.fetch_all(
        """
        SELECT id, kind, status, attempts, max_attempts, last_error, last_failed_at
        FROM background_jobs
        WHERE status IN ('failed','dead')
        ORDER BY COALESCE(last_failed_at, updated_at) DESC, id DESC
        LIMIT %s
        """,
        (limit,),
    )
    if not rows:
        tg.send_message(actor.chat_id, "Failed/dead фоновых заданий нет.")
        return
    lines = [f"Failed/dead jobs: {len(rows)}", ""]
    for row in rows:
        lines.append(
            f"#{row['id']} {row['kind']} · {row['status']} · {row['attempts']}/{row['max_attempts']}\n{_safe_error_text(row.get('last_error'), 220)}"
        )
    tg.send_message(actor.chat_id, "\n".join(lines)[:3900])


def show_author_months(tg: TelegramClient, actor, raw_month: str) -> None:
    if not _require_admin(tg, actor):
        return
    videos = _active_videos()
    rows = build_author_submission_rows(videos)
    month = raw_month.strip()
    if month:
        rows = [row for row in rows if row[0] == month]
    if not rows:
        tg.send_message(actor.chat_id, "За этот период заявок не найдено.")
        return
    if not month:
        months = sorted({row[0] for row in rows}, reverse=True)[:3]
        rows = [row for row in rows if row[0] in months]
    lines = ["Заявки авторов по месяцам", ""]
    current_month = None
    for row in rows:
        if row[0] != current_month:
            current_month = row[0]
            lines.extend([current_month, ""])
        lines.append(
            f"{row[1]}: подано {row[3]}, обработано {row[4]}, одобрено {row[5]}, ждёт {row[6]}, правки {row[7]}"
        )
    tg.send_message(actor.chat_id, "\n".join(lines)[:3900])


def show_payments(tg: TelegramClient, actor, raw_month: str) -> None:
    if not _require_admin(tg, actor):
        return
    ensure_payment_schema()
    month = raw_month.strip() or datetime.now(get_settings().tz).strftime("%Y-%m")
    videos = [video for video in _active_videos() if video.get("status") == "approved"]
    payments = _payment_map()
    due = [video for video in videos if payout_schedule(video)[0] == month]
    paid = [video for video in due if (payments.get(int(video["id"])) or {}).get("is_paid")]
    late = [video for video in due if payout_schedule(video)[1]]
    unpaid = [video for video in due if not (payments.get(int(video["id"])) or {}).get("is_paid")]
    lines = [
        f"Выплаты за {month}",
        "",
        f"К выплате: {len(due)}",
        f"Уже выплачено: {len(paid)}",
        f"Не выплачено: {len(unpaid)}",
        f"Перенесено сюда из-за дедлайна: {len(late)}",
    ]
    if unpaid:
        lines.extend(["", "Не выплачено:"])
        for video in unpaid[:30]:
            name, _ = _author_key(video)
            lines.append(f"#{video['id']} · {name} · {video.get('publish_date') or 'без даты'}")
    tg.send_message(actor.chat_id, "\n".join(lines)[:3900])


def set_paid(tg: TelegramClient, actor, raw_id: str, *, paid: bool) -> None:
    if not _require_admin(tg, actor):
        return
    try:
        video_id = int(raw_id.strip())
    except (TypeError, ValueError):
        tg.send_message(actor.chat_id, "Использование: /paid 325 или /unpaid 325")
        return
    from bot import handlers as h

    video = h.get_video_by_id_outside(video_id)
    if not video or video.get("status") == "deleted":
        tg.send_message(actor.chat_id, f"Видео #{video_id} не найдено.")
        return
    if paid and video.get("status") != "approved":
        tg.send_message(actor.chat_id, f"Видео #{video_id} ещё не approved — выплату отмечать рано.")
        return
    ensure_payment_schema()
    with db.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO video_payments (
                    video_id, is_paid, paid_at, paid_by_tg_id, paid_by_username, updated_at
                )
                VALUES (%s, %s, CASE WHEN %s THEN now() ELSE NULL END, %s, %s, now())
                ON CONFLICT (video_id) DO UPDATE SET
                    is_paid = EXCLUDED.is_paid,
                    paid_at = CASE WHEN EXCLUDED.is_paid THEN now() ELSE NULL END,
                    paid_by_tg_id = EXCLUDED.paid_by_tg_id,
                    paid_by_username = EXCLUDED.paid_by_username,
                    updated_at = now()
                """,
                (video_id, paid, paid, actor.tg_id, actor.username),
            )
        db.log_event(
            conn,
            entity_type="video",
            entity_id=video_id,
            action="payment_marked_paid" if paid else "payment_marked_unpaid",
            actor_tg_id=actor.tg_id,
            actor_username=actor.username,
            after_data={"paid": paid, "payout_month": payout_schedule(video)[0]},
        )
    tg.send_message(
        actor.chat_id,
        f"#{video_id}: {'ВЫПЛАЧЕНО' if paid else 'снова НЕ ВЫПЛАЧЕНО'}. Месяц выплаты: {payout_schedule(video)[0] or '—'}.",
    )


def sync_reporting_command(tg: TelegramClient, actor) -> None:
    if not _require_admin(tg, actor):
        return
    try:
        result = sync_reporting_sheets()
    except Exception as exc:
        tg.send_message(actor.chat_id, f"Не удалось обновить отчёт: {_safe_error_text(exc, 300)}")
        return
    tg.send_message(
        actor.chat_id,
        "Отчёт обновлён.\n"
        f"Видео: {result['videos']}\n"
        f"Строк по авторам: {result['authors_rows']}\n"
        f"Approved для выплат: {result['payment_rows']}\n"
        f"Выплачено: {result['paid']}\n"
        f"Не выплачено: {result['unpaid_approved']}",
    )


def handle_message(message: dict[str, Any]) -> bool:
    actor = _actor(message)
    if not actor:
        return False
    text = (message.get("text") or "").strip()
    if not text.startswith("/"):
        return False
    command, rest = _command_parts(text)
    handled = {
        "/queue_open",
        "/queue_recreate",
        "/video",
        "/errors",
        "/logs",
        "/jobs_failed",
        "/author_months",
        "/payments",
        "/paid",
        "/unpaid",
        "/sync_reporting",
        "/resend_pending",
    }
    if command not in handled:
        return False
    tg = TelegramClient()

    if command == "/queue_open":
        show_active_pending(tg, actor)
    elif command == "/queue_recreate":
        recreate_active_queue_card(tg, actor)
    elif command == "/video":
        show_video(tg, actor, rest)
    elif command == "/errors":
        show_errors(tg, actor, rest)
    elif command == "/logs":
        show_logs(tg, actor, rest)
    elif command == "/jobs_failed":
        show_failed_jobs(tg, actor, rest)
    elif command == "/author_months":
        show_author_months(tg, actor, rest)
    elif command == "/payments":
        show_payments(tg, actor, rest)
    elif command == "/paid":
        set_paid(tg, actor, rest, paid=True)
    elif command == "/unpaid":
        set_paid(tg, actor, rest, paid=False)
    elif command == "/sync_reporting":
        sync_reporting_command(tg, actor)
    elif command == "/resend_pending":
        if not _require_admin(tg, actor):
            return True
        from bot import handlers as h

        h.resend_pending_command(tg, actor)
        show_active_pending(tg, actor, title="Активная заявка после /resend_pending")
    return True
