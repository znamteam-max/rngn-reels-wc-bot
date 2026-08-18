from __future__ import annotations

from datetime import datetime
from typing import Any

from bot import db
from bot.config import get_settings
from bot.telegram import TelegramClient


_INSTALLED = False


def _payment_label(payment: dict[str, Any] | None) -> str:
    return "Выплачено" if bool((payment or {}).get("is_paid")) else "Не выплачено"


def _show_payments(tg: TelegramClient, actor, raw_month: str) -> None:
    from bot import admin_tools as a

    if not a._require_admin(tg, actor):
        return
    a.ensure_payment_schema()
    month = raw_month.strip() or datetime.now(get_settings().tz).strftime("%Y-%m")
    videos = [video for video in a._active_videos() if video.get("status") == "approved"]
    payments = a._payment_map()
    due = [video for video in videos if a.payout_schedule(video)[0] == month]
    paid = [video for video in due if bool((payments.get(int(video["id"])) or {}).get("is_paid"))]
    unpaid = [video for video in due if not bool((payments.get(int(video["id"])) or {}).get("is_paid"))]
    late = [video for video in due if a.payout_schedule(video)[1]]
    lines = [
        f"Выплаты за {month}",
        "",
        f"К выплате: {len(due)}",
        f"Выплачено: {len(paid)}",
        f"Не выплачено: {len(unpaid)}",
        f"Перенесено сюда из-за дедлайна: {len(late)}",
    ]
    if unpaid:
        lines.extend(["", "Не выплачено:"])
        for video in unpaid[:30]:
            name, _ = a._author_key(video)
            lines.append(f"#{video['id']} · {name} · {video.get('publish_date') or 'без даты'}")
    tg.send_message(actor.chat_id, "\n".join(lines)[:3900])


def _wrap_sync(original):
    def sync_reporting_sheets(*, service=None) -> dict[str, Any]:
        result = original(service=service)
        from bot import admin_tools as a

        videos = a._active_videos()
        payments = a._payment_map()
        approved_ids = [int(video["id"]) for video in videos if video.get("status") == "approved"]
        paid = sum(bool((payments.get(video_id) or {}).get("is_paid")) for video_id in approved_ids)
        unpaid = len(approved_ids) - paid
        result["paid"] = paid
        result["unpaid"] = unpaid
        # Compatibility with the v1.0.23 response shape. Under the new policy,
        # absence of a payment row means "not paid", not "unknown".
        result["explicit_unpaid"] = unpaid
        result["unmarked"] = 0
        return result

    return sync_reporting_sheets


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    from bot import admin_tools as a

    a._payment_label = _payment_label
    a.show_payments = _show_payments
    a.sync_reporting_sheets = _wrap_sync(a.sync_reporting_sheets)
    _INSTALLED = True
