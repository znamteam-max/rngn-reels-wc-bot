# RNGN Reels WC Bot - Vercel Finish Report

Version: `1.0.2`
Date: `2026-06-30`
Code Commit: `ebecda0`
Local Project: `C:/Users/znambo/Documents/RNGN`

## Summary

The local project folder was found and linked to the Vercel account that is currently logged in.

Current Vercel access is different from the previous `v1.0.1` report:

```text
Previous scope: rngn1
Current scope: rngn2
Current team: RNGN_WC
Current user: anfisakrasotka-4044
```

The old scope `rngn1` is not available to the current login. The only available project in `rngn2` is:

```text
rngn2/project-dcd2y
Project ID: prj_qKw2tJ6id8d7foSeavrGRRyZHx8n
```

The local folder was linked to that project and deployed to production.

## Production Deployment

```text
Deployment ID: dpl_Hb6CR54ryewENzAR3zDaErm1kboT
Ready state: READY
Production URL: https://project-dcd2y.vercel.app
Deployment URL: https://project-dcd2y-rch2i1ik7-rngn2.vercel.app
Inspect URL: https://vercel.com/rngn2/project-dcd2y/Hb6CR54ryewENzAR3zDaErm1kboT
```

Vercel functions detected:

```text
api/health
api/webhook
```

## Health Status

Production health endpoints are public and reachable:

```text
GET https://project-dcd2y.vercel.app/api/health  -> 200 JSON
GET https://project-dcd2y.vercel.app/api/webhook -> 200 JSON
```

Current runtime `missing_env`:

```text
BOT_TOKEN
WEBHOOK_SECRET
GOOGLE_SERVICE_ACCOUNT_JSON_B64
GOOGLE_SHEETS_SPREADSHEET_ID
```

This means the deployment is alive, but the Telegram bot integration is not ready.

## Vercel Env Status

Present in production:

```text
DATABASE_URL
BOT_USERNAME
WORK_CHAT_ID
ADMIN_CHAT_ID
TIMEZONE
```

Known non-secret values were added:

```text
BOT_USERNAME=rngn_reels_wc_bot
WORK_CHAT_ID=-5425403129
ADMIN_CHAT_ID=-5520370963
TIMEZONE=Europe/Helsinki
```

Missing required secrets/config:

```text
BOT_TOKEN
WEBHOOK_SECRET
GOOGLE_SHEETS_SPREADSHEET_ID
GOOGLE_SERVICE_ACCOUNT_JSON_B64
```

Vercel stores env values as encrypted/sensitive values. In this login, `vercel pull` created env files with empty values, so local scripts could not read `DATABASE_URL` or any other sensitive value. Runtime health confirms `DATABASE_URL` exists in Vercel, but local Neon commands could not be run against the new project from this machine.

## Telegram Status

`getMe` was not run for this project because `BOT_TOKEN` is missing in the current Vercel production env.

Expected bot:

```text
@rngn_reels_wc_bot
```

Webhook was not set because both required values are missing:

```text
BOT_TOKEN
WEBHOOK_SECRET
```

POST check without a configured webhook secret:

```text
POST https://project-dcd2y.vercel.app/api/webhook -> 500
Reason: WEBHOOK_SECRET is not configured
```

This is expected until `WEBHOOK_SECRET` is added.

## Database Status

Neon env names exist in Vercel production, including `DATABASE_URL`.

Local database initialization/count checks were blocked because Vercel did not expose decrypted sensitive env values through `vercel pull` for this login.

Not verified on the new `rngn2/project-dcd2y` database:

```text
scripts/init_db.py
scripts/seed_people.py people.live-seed.csv
scripts/seed_bootstrap_admins.py
people/videos/batches/logs/user_sessions counts
```

Also missing for bootstrap admin:

```text
BOOTSTRAP_SUPERADMIN_IDS or ALLOWED_TELEGRAM_USER_IDS
```

If no superadmin Telegram ID is already in the database, it must be provided and seeded.

## Google Sheets Status

Google Sheets sync is not configured in the current Vercel project.

Missing:

```text
GOOGLE_SHEETS_SPREADSHEET_ID
GOOGLE_SERVICE_ACCOUNT_JSON_B64
```

Because these are missing, the bot cannot be considered production-ready for the full reporting workflow.

## Local Verification

Passed:

```text
python -B -m unittest discover -s tests
python -B -m compileall -q api bot scripts tests
```

Result:

```text
6 tests passed
compile check passed
```

Vercel runtime logs:

```text
vercel logs https://project-dcd2y.vercel.app --scope rngn2 --since 1h --level error
Result: No logs found for rngn2/project-dcd2y
```

## Live E2E Status

Main E2E scenario was not run.

Blocked scenario:

```text
user sends /start
user sends /new_video
request arrives in admin chat
admin approves
row appears in Neon
row appears in Google Sheets
final card appears in work chat
```

Blocking reasons:

```text
BOT_TOKEN is missing
WEBHOOK_SECRET is missing
Google Sheets env is missing
local decrypted DATABASE_URL is unavailable for init/seed/counts
superadmin Telegram ID is not available in current env
```

## Next Required Inputs

Add these to Vercel production for `rngn2/project-dcd2y`:

```text
BOT_TOKEN=<token for @rngn_reels_wc_bot>
WEBHOOK_SECRET=<secret token for Telegram webhook>
GOOGLE_SHEETS_SPREADSHEET_ID=<spreadsheet id>
GOOGLE_SERVICE_ACCOUNT_JSON_B64=<base64 service account json>
```

If no admin is already seeded, also provide:

```text
BOOTSTRAP_SUPERADMIN_IDS=<Telegram numeric user id>
```

After those are added:

1. Pull/check env names again.
2. Re-deploy if Vercel requires it for updated env.
3. Run `getMe` and confirm `rngn_reels_wc_bot`.
4. Set webhook to `https://project-dcd2y.vercel.app/api/webhook`.
5. Initialize/seed Neon or add an admin-only init path.
6. Run the Telegram E2E scenario.
7. Confirm Neon row, Google Sheets row, and work-chat final card.

## Readiness

Integration is not complete yet.

The code is deployed and reachable, but production setup is blocked by missing Telegram and Google Sheets secrets.
