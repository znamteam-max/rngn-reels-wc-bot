# RNGN Reels WC Bot - GitHub/Vercel/E2E Report

Version: `1.0.4`
Date: `2026-06-30`
Local Project: `C:/Users/znambo/Documents/RNGN`
Code Commit: `d85d971`

## GitHub

Repository:

```text
https://github.com/znamteam-max/rngn-reels-wc-bot.git
```

Status:

```text
Code pushed: yes
Branch: main
Remote main commit after code cleanup: d85d971931da0cf118a3c95b2d9591fca8b28fc0
Force push used: no
```

Changes made before push:

```text
.gitignore hardened for local env files, service account JSON, secret files, caches, and .venv.
```

Temporary operation commits:

```text
96cdd23 Add protected runtime init endpoint
d85d971 Remove runtime init endpoint
```

The temporary runtime init endpoint was removed from the final production code.

## Vercel

Active Vercel context:

```text
User: anfisakrasotka-4044
Scope: rngn2
Team: RNGN_WC
Project: project-dcd2y
Project ID: prj_qKw2tJ6id8d7foSeavrGRRyZHx8n
```

Production URL:

```text
https://project-dcd2y.vercel.app
```

Git integration:

```text
Connected: yes
Provider: GitHub
Repo: znamteam-max/rngn-reels-wc-bot
Production branch: main
Latest Git deployment commit: d85d971
```

Latest production deployment:

```text
Deployment ID: dpl_HXdxuYq9cca2BaoPTy5feRyx9BJh
Ready state: READY
Deployment URL: https://project-dcd2y-pneffrvc4-rngn2.vercel.app
Aliases:
- https://project-dcd2y.vercel.app
- https://project-dcd2y-rngn2.vercel.app
- https://project-dcd2y-git-main-rngn2.vercel.app
```

Functions in final deployment:

```text
api/health
api/webhook
```

## Env

Runtime health confirms all required env groups are available:

```text
missing_env: []
```

Required env verified by runtime and local pull where available:

```text
BOT_TOKEN
WEBHOOK_SECRET
GOOGLE_SHEETS_SPREADSHEET_ID
GOOGLE_SERVICE_ACCOUNT_JSON_B64
DATABASE_URL
BOT_USERNAME
WORK_CHAT_ID
ADMIN_CHAT_ID
TIMEZONE
```

Important note:

```text
Some secrets are provided through Vercel Shared Variables.
They may not all appear in `vercel env ls`, but runtime health sees them.
No secret values were printed.
```

Missing for admin bootstrap:

```text
BOOTSTRAP_SUPERADMIN_IDS or ALLOWED_TELEGRAM_USER_IDS
```

## Health

Checked:

```text
GET https://project-dcd2y.vercel.app/api/health
GET https://project-dcd2y.vercel.app/api/webhook
```

Result:

```text
200 JSON
missing_env: []
```

Webhook POST protection:

```text
POST /api/webhook without secret -> 401
POST /api/webhook with secret    -> 200 {"ok":true}
```

## Telegram

`getMe` result:

```text
ok: true
bot_username: rngn_reels_wc_bot
```

Webhook status:

```text
url: https://project-dcd2y.vercel.app/api/webhook
pending_update_count: 0
allowed_updates: message,callback_query
last_error_message: None
```

Webhook was set for the correct bot.

## Neon

Because local `DATABASE_URL` remains hidden by Vercel for this login, database init was done through a temporary Vercel runtime endpoint protected by `WEBHOOK_SECRET`.

The endpoint was used only for init/seed/counts and was removed from the final deployment.

Init/seed result:

```text
Schema initialized: yes
people seed inserted: 3
people seed updated: 0
bootstrap admin ids present: 0
```

Table counts after init/seed:

```text
people: 3
videos: 0
batches: 0
logs: 0
user_sessions: 0
```

Role counts:

```text
author: 1
montage: 1
voice: 1
admin: 0
superadmin: 0
```

## Google Sheets

Read-only Google Sheets access was verified with the configured service account.

Result:

```text
sheets_access_ok: true
Videos tab present: true
Visible tabs: Videos, People, Drafts, Batches, Logs, Stats
```

Google Sheets write sync was not fully tested because no video was approved through the Telegram admin flow.

## Live E2E

Main live E2E was not completed.

Not completed:

```text
/start
/new_video
Instagram/Reels link submission
admin approval
Neon approved video row
Google Sheets Videos row
final card in WORK_CHAT_ID
duplicate check
```

Reason:

```text
No admin or superadmin Telegram user ID is available in the current DB/env.
Without an admin user, the approval step cannot be completed.
Also, the full flow requires a real Telegram user interaction.
```

## Runtime Logs

Checked:

```text
vercel logs https://project-dcd2y.vercel.app --scope rngn2 --since 1h --level error
```

Result:

```text
No logs found for rngn2/project-dcd2y
```

## Remaining Blockers

1. Add a numeric Telegram user ID for at least one admin:

```text
BOOTSTRAP_SUPERADMIN_IDS=<numeric Telegram user id>
```

or add that user through DB/admin tooling.

2. Re-run admin seed after the ID is available.

3. Complete the live Telegram scenario:

```text
user submits request
request arrives in admin chat
admin approves
row appears in Neon videos
row appears in Google Sheets Videos
final card appears in WORK_CHAT_ID
duplicate link is rejected by shortcode
```

## Readiness

Integration is not fully complete yet.

The code is pushed to GitHub, Vercel is connected to GitHub, production health is clean, the correct Telegram bot webhook is installed, Neon schema is initialized, and Google Sheets access works.

The remaining production readiness blocker is the live admin/E2E flow.
