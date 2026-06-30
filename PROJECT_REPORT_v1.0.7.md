# RNGN Reels WC Bot - Admin-Only Chat ID + Resend Report

Version: `1.0.7`
Date: `2026-06-30`
Local Project: `C:/Users/znambo/Documents/RNGN`
GitHub Repo: `https://github.com/znamteam-max/rngn-reels-wc-bot`
Production URL: `https://project-dcd2y.vercel.app`
Bot: `@rngn_reels_wc_bot`

## Code And Deploy

Main product commit:

```text
f52a7f3 Release v1.0.7 admin-only Telegram flow
```

Final clean Vercel deployment:

```text
Deployment ID: dpl_DS1XwthqT3Hmu9APxYaFZcwczMRr
Status: READY
Deployment URL: https://project-dcd2y-m672wy1le-rngn2.vercel.app
Alias: https://project-dcd2y.vercel.app
Functions:
- api/health
- api/webhook
```

Temporary protected runtime endpoints were used only to migrate/check production DB and were removed from final production code. Final check confirms `/api/runtime_admin` returns `404`.

## Product Changes

- Removed `WORK_CHAT_ID` from the product flow and required env checks.
- Review and approved cards now go only to `ADMIN_CHAT_ID`.
- Added `/chatid`, usable in private, group, and supergroup chats, including `/chatid@rngn_reels_wc_bot`.
- Added `/resend_pending` admin command and admin menu button.
- Pending review cards store `admin_message_chat_id`, `admin_message_id`, and `admin_notified_at`.
- Approval edits the stored admin review card to the approved card when possible, otherwise sends a new approved card to `ADMIN_CHAT_ID`.
- Telegram admin delivery failures log `admin_notify_failed` with chat id, status code, and Telegram description.
- If submit succeeds but admin notification fails, the user gets a private warning with recovery instructions.
- `/new_video` is private-chat only; group calls instruct the user to open the bot in private.
- Montage step now has `Смонтировал сам автор`; the DB stores `montage_same_as_author`.
- Person names are displayed as `Name (@username)` or `Name (ник не указан)` in buttons, cards, and Google Sheets rows.
- Cards no longer show empty `Проверил`, `Создано`, or empty `Комментарий` lines.
- Added `scripts/setup_bot_ui.py` to configure Telegram commands and menu button.
- Project version updated to `1.0.7` in `pyproject.toml` and `uv.lock`.

## Database Migration

Production DB migration completed successfully.

Added/verified columns on `videos`:

```text
author_username
montage_username
montage_same_as_author
voice_username
admin_message_chat_id
admin_message_id
admin_notified_at
```

Backfilled username snapshot columns from `people` where possible.

Production status counts during migration:

```text
approved: 1
pending: 4
```

After fixing `ADMIN_CHAT_ID` and running `/resend_pending`, all pending cards have stored admin messages:

```text
pending: 4
pending_with_admin_message: 4
```

## Vercel / Telegram Live Fix

Found production `ADMIN_CHAT_ID` was set to an unreachable migrated id:

```text
-1005520370963 -> Telegram 400 Bad Request: chat not found
```

Updated Vercel production `ADMIN_CHAT_ID` to the reachable group id:

```text
-5520370963
```

Telegram `getChat` confirms the current admin chat is reachable:

```text
ok: true
type: group
```

Telegram commands/menu configured by `scripts/setup_bot_ui.py`.

Configured command count:

```text
7
```

Commands:

```text
/start
/new_video
/my_requests
/help
/admin
/chatid
/resend_pending
```

## Verification

Local checks:

```text
python -m py_compile bot\handlers.py bot\config.py bot\messages.py bot\telegram.py bot\sheets.py scripts\init_db.py scripts\check_env.py scripts\setup_bot_ui.py api\webhook.py api\health.py
python -m unittest tests.test_links
git diff --check
```

Result:

```text
15 tests passed
py_compile passed
git diff --check passed
```

Live checks:

```text
GET https://project-dcd2y.vercel.app/api/health  -> ok=true, missing_env=[]
GET https://project-dcd2y.vercel.app/api/webhook -> ok=true, missing_env=[]
GET https://project-dcd2y.vercel.app/api/runtime_admin -> 404
Telegram getWebhookInfo -> https://project-dcd2y.vercel.app/api/webhook
Telegram pending_update_count -> 0
Telegram getMyCommands -> 7 commands
Telegram getChat(-5520370963) -> ok=true, type=group
```

`/resend_pending` was invoked through the production webhook after the env fix:

```text
webhook response: {"ok": true}
pending_with_admin_message: 4
```

## Notes For Next Handoff

- `WORK_CHAT_ID` can remain in old Vercel/env history, but the app no longer reads or requires it.
- If the admin group changes again, run `/chatid@rngn_reels_wc_bot` in that group, update `ADMIN_CHAT_ID` in Vercel, redeploy, then run `/resend_pending`.
- Historical `admin_notify_failed` logs from the broken `-1005520370963` value are expected and useful as proof of diagnostics.
