# RNGN Reels WC Bot - Admin Date + People Seed E2E Report

Version: `1.0.6`
Date: `2026-06-30`
Local Project: `C:/Users/znambo/Documents/RNGN`
GitHub Repo: `https://github.com/znamteam-max/rngn-reels-wc-bot`
Production URL: `https://project-dcd2y.vercel.app`

## Code And Deploy

Main product commit:

```text
dbd8c00 Move publish date to admin review
```

Final clean production code commit:

```text
89660ee Remove runtime seed endpoint
```

Final clean Vercel deployment:

```text
Deployment ID: dpl_DT5hDQ7EcCRgnovTp1PzqTJwx58G
Status: READY
Deployment URL: https://project-dcd2y-pm4lchja9-rngn2.vercel.app
Aliases:
- https://project-dcd2y.vercel.app
- https://project-dcd2y-rngn2.vercel.app
- https://project-dcd2y-git-main-rngn2.vercel.app
Functions:
- api/health
- api/webhook
```

Temporary runtime seed/verification endpoint was used during setup and removed from the final production code.

## Product Changes

Participant `/new_video` flow was changed:

```text
Instagram/Reels link
duplicate check
author
voice
montage
YouTube / skip
TikTok / skip
VK / skip
preview
send to review
```

Participant publication date step was removed. New pending requests can have:

```text
publish_date = NULL
```

Admin review now includes:

```text
Указать дату
Одобрить
Правка
Дубль
Удалить
Назад
Дальше
```

Admin date input supports:

```text
Сегодня
Вчера
Позавчера
Manual: YYYY-MM-DD, DD.MM, D.M
```

Approval is blocked until `publish_date` is set. The database update also requires `publish_date IS NOT NULL`.

Migration-safe audit fields were added:

```text
publish_date_set_by_tg_id bigint null
publish_date_set_by_username text null
publish_date_set_at timestamptz null
```

## Verification

Local checks passed:

```text
python -B -m unittest discover -s tests
Result: 8 tests passed

python -B -m compileall -q api bot scripts tests
Result: passed
```

Health checks:

```text
GET /api/health  -> 200 JSON, missing_env: []
GET /api/webhook -> 200 JSON, missing_env: []
```

Telegram:

```text
getMe_ok: true
bot_username: rngn_reels_wc_bot
webhook_url: https://project-dcd2y.vercel.app/api/webhook
pending_update_count: 0
last_error_message: None
```

Vercel error logs:

```text
No logs found for rngn2/project-dcd2y
```

## People Seed

Production people seed was applied from `people.live-seed.csv`.

Active role counts:

```text
author: 10
montage: 13
voice: 5
superadmin: 1
```

Total rows include inactive old test seed rows:

```text
people total: 32
author total/active: 11 / 10
montage total/active: 14 / 13
voice total/active: 6 / 5
superadmin total/active: 1 / 1
```

Bootstrap superadmin:

```text
BOOTSTRAP_SUPERADMIN_IDS present: yes
bootstrap admin IDs used: 1
numeric ID not printed
```

## E2E Scenario

Live webhook E2E was executed through the production webhook using the real bot token/secret and production database.

Test shortcode:

```text
CODEXV106182144
```

Participant flow result:

```text
Submitted video id: 2
Batch id: 2
After submit status: pending
After submit publish_date: NULL
```

This confirms the participant date step was not required for submission.

Admin approval without date:

```text
Action: approve before date
Result: blocked
Video status after blocked approve: pending
publish_date after blocked approve: NULL
```

Admin date setting:

```text
Action: set date = today
Result: publish_date set
publish_date: 2026-06-30
```

Final approval:

```text
Video status: approved
publish_date: 2026-06-30
sheet_row: 2
```

Google Sheets:

```text
Videos row found: yes
Sheet row: 2
Sheet status: approved
Sheet publish_date: 2026-06-30
```

Duplicate check:

```text
Same Instagram/Reels URL submitted again
videos count before duplicate: 2
videos count after duplicate: 2
duplicate recent matches: 1
Result: duplicate detected, no new video row created
```

## Remaining Blocker

The full production Telegram chat chain did not pass because the configured chat IDs are not reachable by the bot.

Direct Telegram `sendMessage` checks:

```text
ADMIN_CHAT_ID=-5520370963 -> Bad Request: chat not found
WORK_CHAT_ID=-5425403129  -> Bad Request: chat not found
```

Database logs for the test video include:

```text
admin_notify_failed
work_chat_notify_failed
```

So the bot can process the request, block approval before date, set date, approve, write Neon, write Google Sheets, and reject duplicates. It cannot currently deliver the admin request card or final approved card to the configured Telegram chats.

## Current Runtime Counts After E2E

```text
people: 32
videos: 2
batches: 2
logs: 12
user_sessions: 0
```

Recent test video:

```text
id: 2
status: approved
publish_date: 2026-06-30
instagram_id: CODEXV106182144
batch_id: 2
sheet_row: 2
```

## Next Required Fix

Fix Telegram chat delivery:

```text
1. Add @rngn_reels_wc_bot to the real admin chat and work chat.
2. Send a message in each chat.
3. Get the real chat IDs from Telegram updates or bot logs.
4. Update Vercel production:
   ADMIN_CHAT_ID=<real reachable admin chat id>
   WORK_CHAT_ID=<real reachable work chat id>
5. Redeploy/retest.
```

For supergroups, the real ID often starts with `-100...`; the currently configured IDs may be missing that prefix or may point to chats where the bot is not a member.

## Readiness

Not production-ready yet.

The application logic, database migration, people seed, admin-date approval rule, Google Sheets write, and duplicate detection passed. The remaining blocker is Telegram delivery to the configured admin/work chats.
