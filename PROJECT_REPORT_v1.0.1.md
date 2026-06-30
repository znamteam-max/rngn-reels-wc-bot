# RNGN Reels WC Bot - Live Integration Report

Version: `1.0.1`  
Date: `2026-06-30`  
Code Commit: `6e9d29c`  
Project: `rngn1/znambo-telegram-assistant`  
Production URL: `https://znambo-telegram-assistant-rngn1.vercel.app`

## What Was Checked

- Vercel project was linked locally.
- Vercel production env names were checked without printing secret values.
- Neon schema was initialized with `scripts/init_db.py`.
- Test people were seeded from `people.live-seed.csv`.
- Bootstrap superadmin was seeded from existing admin IDs in env without printing IDs.
- Production deploy was completed and promoted to alias.
- Vercel Authentication was disabled for this project because Telegram webhook needs public access.
- `GET /api/health` returns bot health JSON.
- `GET /api/webhook` returns bot health JSON.
- Webhook secret header protection was tested:
  - POST without `X-Telegram-Bot-Api-Secret-Token` returns `401`.
  - POST with the secret header returns `200`.
- Telegram `setWebhook` and `getWebhookInfo` were executed without printing token/secret values.
- Runtime error scan showed no Vercel error logs for the new deployment window.

## Vercel Status

Production deployment:

```text
Deployment ID: dpl_APHtp336PMLrn3idt6tZQgxXA8s4
Ready state: READY
Alias assigned: true
Production URL: https://znambo-telegram-assistant-rngn1.vercel.app
Functions:
- api/health
- api/webhook
```

Health responses:

```text
GET /api/health  -> 200 JSON
GET /api/webhook -> 200 JSON
```

Current health warning:

```text
missing_env:
- GOOGLE_SERVICE_ACCOUNT_JSON_B64
- GOOGLE_SHEETS_SPREADSHEET_ID
```

## Webhook Status

Webhook was set successfully:

```text
url: https://znambo-telegram-assistant-rngn1.vercel.app/api/webhook
allowed_updates: message, callback_query
pending_update_count: 0
last_error_message: None
```

Important blocker:

```text
Current Telegram token belongs to @znambo_personal_assistant_bot,
not @rngn_reels_wc_bot.
```

The requested bot is `@rngn_reels_wc_bot`, so production Telegram E2E must not be considered complete until the correct bot token is added to Vercel.

## Database Status

Neon schema initialization completed.

Current aggregate counts:

```text
people: 4
videos: 0
batches: 0
logs: 0
user_sessions: 0
```

Seeded roles:

```text
author: 1
montage: 1
voice: 1
superadmin: 1
```

No personal Telegram IDs were printed.

## Commands/Flows Verified

Verified at code/build level:

- `/start`
- `/new_video`
- `/my_requests`
- `/admin`
- `/summary`
- `/calendar`
- `/people`
- `/search`
- `/help`
- `/sync_sheets`
- `/add_person`
- `/activate_person`
- `/deactivate_person`
- `/edit_video`

Verified live:

- Vercel health endpoints
- webhook public access
- webhook secret-token rejection/acceptance
- Telegram webhook registration against the currently configured token
- Neon schema and seed scripts

Not fully verified live because of blockers:

- Full Telegram `/new_video` flow for `@rngn_reels_wc_bot`
- Admin approval from Telegram
- Google Sheets row creation
- Final card in `WORK_CHAT_ID`

## Live Integration Blockers

1. Correct Telegram token is missing.

   Vercel currently has `TELEGRAM_BOT_TOKEN`, but live `getMe` shows:

   ```text
   bot_username=znambo_personal_assistant_bot
   ```

   Required:

   ```text
   BOT_TOKEN or TELEGRAM_BOT_TOKEN for @rngn_reels_wc_bot
   ```

2. Google Sheets env vars are missing in Vercel production:

   ```text
   GOOGLE_SHEETS_SPREADSHEET_ID
   GOOGLE_SERVICE_ACCOUNT_JSON_B64
   ```

3. Vercel `TZ` env var cannot be created because Vercel reserves that name.

   Applied replacement:

   ```text
   TIMEZONE=Europe/Helsinki
   ```

   The code accepts `TIMEZONE`, `TZ`, and `DEFAULT_TIMEZONE`.

## Risks

- The existing Vercel project was previously a different assistant app. It is now deployed as the RNGN Reels bot project.
- The existing Telegram token is for another bot, so webhook changes currently affect that bot token.
- Google Sheets sync fallback is implemented in code, but live Sheets success cannot be verified until Google env vars are added.
- If Vercel Authentication is re-enabled for `.vercel.app` URLs, Telegram webhook delivery will fail.

## Next Required Inputs

To complete the main end-to-end scenario, provide or set in Vercel:

```text
BOT_TOKEN=<token for @rngn_reels_wc_bot>
GOOGLE_SHEETS_SPREADSHEET_ID=<spreadsheet id>
GOOGLE_SERVICE_ACCOUNT_JSON_B64=<base64 service account json>
```

After those are set:

1. Pull/check env names again.
2. Re-run health.
3. Re-run `setWebhook`.
4. Test `/start`.
5. Test `/new_video`.
6. Approve from admin chat.
7. Confirm Neon row, Google Sheets row, and final work-chat card.
