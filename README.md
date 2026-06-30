# RNGN Reels WC Bot

Production-ready Telegram bot for Reels монтаж reporting during ЧМ-2026.

Bot: `@rngn_reels_wc_bot`  
Work chat: `-5425403129`  
Admin chat: `-5520370963`  
Runtime: Python 3.12 on Vercel Serverless Functions  
Database: Neon Postgres via `DATABASE_URL`  
Report: Google Sheets tab `Videos`

## Structure

```text
api/webhook.py          Telegram webhook and GET health
api/health.py           Separate health endpoint
bot/                   Bot modules
scripts/init_db.py      Neon schema bootstrap
scripts/seed_people.py  People/admin seed loader
people.example.csv      Seed file example
requirements.txt        Python dependencies
vercel.json             Vercel function config
```

## Environment Variables

Copy `.env.example` and fill values locally. In Vercel, add the same variables in Project Settings.

```text
BOT_TOKEN=Telegram bot token from BotFather
BOT_USERNAME=rngn_reels_wc_bot
WEBHOOK_SECRET=random long secret for Telegram webhook header
DATABASE_URL=Neon Postgres connection string
WORK_CHAT_ID=-5425403129
ADMIN_CHAT_ID=-5520370963
GOOGLE_SERVICE_ACCOUNT_JSON_B64=base64 encoded service account JSON
GOOGLE_SHEETS_SPREADSHEET_ID=Google Spreadsheet ID
TZ=Europe/Helsinki
BOOTSTRAP_SUPERADMIN_IDS=comma-separated Telegram IDs for first setup
```

`BOT_TOKEN`, `DATABASE_URL`, `WEBHOOK_SECRET`, and `GOOGLE_SERVICE_ACCOUNT_JSON_B64` must never be printed or committed.

The bot also accepts legacy aliases `TELEGRAM_BOT_TOKEN`, `TELEGRAM_BOT_USERNAME`, `TELEGRAM_WEBHOOK_SECRET`, `TIMEZONE`, and `DEFAULT_TIMEZONE`.

## Local Setup

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Initialize Neon:

```powershell
$env:DATABASE_URL="postgresql://..."
python scripts/init_db.py
```

Seed people:

```powershell
python scripts/seed_people.py people.example.csv
```

Check env presence without printing secret values:

```powershell
python scripts/check_env.py
```

You can also use a simple file with one name per line:

```powershell
python scripts/seed_people.py authors.txt --role author
```

## Google Sheets

The spreadsheet must contain a tab named `Videos` with these columns:

```text
id,status,publish_date,instagram_url,instagram_id,youtube_url,tiktok_url,vk_url,author,author_tg_id,montage,montage_tg_id,voice,voice_tg_id,added_by,checked_by,created_at,checked_at,batch_id,comment
```

Create a Google Cloud service account, enable Google Sheets API, and share the spreadsheet with the service account email as Editor.

Encode the service account JSON for Vercel:

```powershell
[Convert]::ToBase64String([IO.File]::ReadAllBytes(".\service-account.json"))
```

Put that value into `GOOGLE_SERVICE_ACCOUNT_JSON_B64`.

## Vercel Deploy

The project uses `api/webhook.py` and `api/health.py` as Python functions. `vercel.json` excludes the old Cloudflare/Node assets from the Python bundle.

Deploy:

```powershell
vercel deploy --prod
```

Health checks:

```powershell
curl https://YOUR-VERCEL-DOMAIN.vercel.app/api/health
curl https://YOUR-VERCEL-DOMAIN.vercel.app/api/webhook
```

Both return JSON and include only missing environment variable names, never secret values.

## Telegram Webhook

Set webhook with secret token:

```powershell
curl "https://api.telegram.org/bot$env:BOT_TOKEN/setWebhook" `
  -d "url=https://YOUR-VERCEL-DOMAIN.vercel.app/api/webhook" `
  -d "secret_token=$env:WEBHOOK_SECRET" `
  -d "allowed_updates=[\"message\",\"callback_query\"]"
```

Telegram will send `X-Telegram-Bot-Api-Secret-Token`; the webhook rejects requests when it does not match `WEBHOOK_SECRET`.

## Bot Commands

User commands:

```text
/start
/new_video
/my_requests
/help
```

Admin commands:

```text
/admin
/summary
/calendar
/people
/search
/sync_sheets
/edit_video id field value
```

Superadmin commands:

```text
/add_person role name [tg_id] [@username]
/activate_person id
/deactivate_person id
```

Roles: `author`, `montage`, `voice`, `admin`, `superadmin`.

## Main Flow

`/new_video` asks for Instagram/Reels URL, publication date, author, optional voice, one editor, and optional YouTube/TikTok/VK links. Instagram duplicates are detected by shortcode from `/reel/{id}`, `/p/{id}`, or `/tv/{id}`.

After preview, the user sends the request to review. The video becomes `pending`, gets a `batch_id`, and admins see a single card or a batch summary. Admin approval is atomic: the database update only succeeds while status is still `pending`, so two admins cannot approve the same video with a conflict.

After approval:

1. `videos.status` becomes `approved`.
2. `checked_by_*` and `checked_at` are set.
3. The `Videos` sheet row is inserted or updated by `id`.
4. The final card is sent to `WORK_CHAT_ID`.

If Google Sheets is temporarily unavailable, the video remains `approved`; the failure is recorded in `logs` as `sync_sheets_failed`. Run `/sync_sheets` later to upsert approved videos again.

## Supported Link Normalization

```text
Instagram: /reel/{id}, /p/{id}, /tv/{id}
YouTube: youtu.be/{id}, youtube.com/watch?v={id}, /shorts/{id}
TikTok: /video/{id}, otherwise stores cleaned URL
VK: clip/video IDs when present, otherwise stores cleaned URL
```

Tracking query parameters such as `utm_*`, `igsh*`, `si`, `fbclid`, and `gclid` are removed.
