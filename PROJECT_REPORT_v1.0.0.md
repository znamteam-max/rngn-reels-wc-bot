# RNGN Reels WC Bot — Project Report

Version: `1.0.0`  
Date: `2026-06-30`  
Project: Telegram bot `@rngn_reels_wc_bot` for Reels монтаж reporting during ЧМ-2026.

## What Was Built

Production-ready Python 3.12 Telegram bot project for Vercel Serverless Functions.

Core stack:

- Vercel Python serverless handlers
- Neon Postgres via `DATABASE_URL`
- Telegram Bot API via `requests`
- Google Sheets API via service account
- `psycopg3` for database access

## Main Files

- `api/webhook.py` — Telegram webhook, secret-token check, GET health JSON
- `api/health.py` — separate health endpoint
- `bot/handlers.py` — bot commands, form flow, admin queue, approvals
- `bot/db.py` — Postgres helpers and session storage
- `bot/links.py` — Instagram/YouTube/TikTok/VK normalization
- `bot/sheets.py` — Google Sheets upsert by video `id`
- `bot/telegram.py` — Telegram API client
- `scripts/init_db.py` — Neon schema initialization
- `scripts/seed_people.py` — people/admin seed loader
- `people.example.csv` — seed example
- `requirements.txt` — Python dependencies
- `vercel.json` — Vercel deployment config
- `README.md` — deployment and operation guide

## Implemented Bot Commands

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

## Implemented Flows

- New video multi-step form
- Instagram duplicate detection by shortcode
- Manual and list-based people selection
- Optional YouTube/TikTok/VK links
- Request preview before submission
- Pending requests grouped into admin batches
- Admin queue with one-card navigation
- Atomic approval to avoid double-approval conflicts
- Revision, duplicate, and delete statuses
- User correction flow for `needs_revision`
- Google Sheets insert/update by `id`
- Fallback logging when Google Sheets sync fails
- Manual `/sync_sheets` retry
- Final approved video card to `WORK_CHAT_ID`

## Database

`scripts/init_db.py` creates:

- `people`
- `videos`
- `batches`
- `admin_locks`
- `logs`
- `user_sessions`

Plus indexes required for:

- `videos(instagram_id)`
- `videos(status)`
- `videos(publish_date)`
- `videos(batch_id)`
- `people(role, is_active)`

`user_sessions` was added because Vercel functions are stateless and the `/new_video` form needs durable multi-step state.

## Verification

Passed locally:

```powershell
python -B -m unittest discover -s tests
python -B -m compileall -q api bot scripts tests
python -c "import api.webhook, bot.handlers; print('imports ok')"
```

Notes:

- Live Telegram, Neon, and Google Sheets integration was not run because production secrets are not configured in this workspace.
- No real secret values were added to files.

## Deployment Checklist

1. Add Vercel environment variables from `.env.example`.
2. Run `python scripts/init_db.py` with `DATABASE_URL`.
3. Seed people with `python scripts/seed_people.py people.example.csv`.
4. Share the Google Sheet with the service account email.
5. Deploy to Vercel.
6. Check:

```text
GET /api/health
GET /api/webhook
```

7. Set Telegram webhook with `secret_token`.
8. Test `/start`, `/new_video`, admin approval, Google Sheets row, and final work-chat card.

