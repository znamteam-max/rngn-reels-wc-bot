# RNGN Reels WC Bot

Production-ready Telegram bot for Reels монтаж reporting during ЧМ-2026.

Bot: `@rngn_reels_wc_bot`  
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
scripts/setup_bot_ui.py Telegram command/menu setup
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
ADMIN_CHAT_ID=-5520370963
GOOGLE_SERVICE_ACCOUNT_JSON_B64=base64 encoded service account JSON
GOOGLE_SHEETS_SPREADSHEET_ID=Google Spreadsheet ID
YOUTUBE_API_KEY=optional YouTube Data API v3 key for metrics
CRON_SECRET=optional secret for /api/cron/youtube-metrics
TZ=Europe/Helsinki
BOOTSTRAP_SUPERADMIN_IDS=comma-separated Telegram IDs for first setup
```

`BOT_TOKEN`, `DATABASE_URL`, `WEBHOOK_SECRET`, `GOOGLE_SERVICE_ACCOUNT_JSON_B64`, `YOUTUBE_API_KEY`, and `CRON_SECRET` must never be printed or committed.

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

Production people seed:

```powershell
python scripts/seed_people.py people.live-seed.csv
```

Check env presence without printing secret values:

```powershell
python scripts/check_env.py
```

Configure Telegram commands and the bot menu:

```powershell
python scripts/setup_bot_ui.py
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

YouTube metrics sync uses a separate tab named `MetricsRaw`. The bot creates it when possible and appends successful daily YouTube snapshots with these columns:

```text
captured_at,video_id,platform,platform_video_id,views,likes,comments,shares,source_status,error_message,instagram_url,youtube_url,author,montage,voice
```

## Vercel Deploy

The project uses `api/webhook.py`, `api/health.py`, and `api/cron/youtube-metrics.py` as Python functions. `vercel.json` excludes the old Cloudflare/Node assets from the Python bundle and schedules YouTube metrics sync daily at `0 3 * * *`.

Deploy:

```powershell
vercel deploy --prod
```

Health checks:

```powershell
curl https://YOUR-VERCEL-DOMAIN.vercel.app/api/health
curl https://YOUR-VERCEL-DOMAIN.vercel.app/api/webhook
```

Both return JSON and include only missing environment variable names, never secret values. `YOUTUBE_API_KEY` and `CRON_SECRET` are reported as optional missing env names and do not block the main bot.

Cron metrics endpoint:

```text
GET /api/cron/youtube-metrics
Authorization: Bearer <CRON_SECRET>
```

If `CRON_SECRET` is missing, the endpoint returns `500` with `CRON_SECRET not configured`. If the authorization header is wrong, it returns `401`.

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
/chatid
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
/resend_pending
/sync_youtube_metrics
/metrics_youtube_today
/metrics_youtube_all
/metrics_video id
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

`/new_video` works only in a private chat with the bot. In groups, the bot asks the user to open a private chat. The form asks for Instagram/Reels URL, author, optional voice, one editor, and optional YouTube/TikTok/VK links. The montage step includes `Смонтировал сам автор`. Participants do not set the publication date. Instagram duplicates are detected by shortcode from `/reel/{id}`, `/p/{id}`, or `/tv/{id}`.

After preview, the user sends the request to review. The video becomes `pending`, gets a `batch_id`, and the bot sends a review card only to `ADMIN_CHAT_ID`. The stored admin Telegram message is saved in `admin_message_chat_id`, `admin_message_id`, and `admin_notified_at`. Admins can run `/resend_pending` to send every pending card to the current admin chat again. The admin card shows whether `publish_date` is set. Admins must set the publication date during review before approval; approval is blocked until the date is present. Admin approval is atomic: the database update only succeeds while status is still `pending` and `publish_date` is not null, so two admins cannot approve the same video with a conflict.

Admin date controls support quick presets for today, yesterday, and the day before yesterday, plus manual input in `YYYY-MM-DD`, `DD.MM`, or `D.M` format. `DD.MM` and `D.M` use the current year from `TIMEZONE`.

After approval:

1. `videos.status` becomes `approved`.
2. `checked_by_*` and `checked_at` are set.
3. The `Videos` sheet row is inserted or updated by `id`.
4. The original admin review card is edited to an approved card when possible; otherwise a new approved card is sent to `ADMIN_CHAT_ID`.

If Google Sheets is temporarily unavailable, the video remains `approved`; the failure is recorded in `logs` as `sync_sheets_failed`. Run `/sync_sheets` later to upsert approved videos again.

Use `/chatid` in the target admin group or supergroup to get the real `chat_id`, then update `ADMIN_CHAT_ID` in Vercel and run `/resend_pending`.

## YouTube Metrics

Only YouTube metrics are supported in the first metrics module. Instagram, TikTok, and VK metrics are intentionally untouched.

`/sync_youtube_metrics` is admin-only. It selects approved videos with `youtube_url`, extracts or fills `youtube_id`, requests YouTube Data API v3 statistics in batches of 50 IDs, stores one daily snapshot per video in `video_metrics_snapshots`, updates latest values on `videos`, and appends successful rows to `MetricsRaw`.

If `YOUTUBE_API_KEY` is missing, `/sync_youtube_metrics` replies:

```text
YOUTUBE_API_KEY не настроен. Добавь ключ в Vercel env.
```

Admin metrics commands:

```text
/sync_youtube_metrics
/metrics_youtube_today
/metrics_youtube_all
/metrics_video <video_id>
```

## Supported Link Normalization

```text
Instagram: /reel/{id}, /p/{id}, /tv/{id}
YouTube: youtu.be/{id}, youtube.com/watch?v={id}, /shorts/{id}
TikTok: /video/{id}, otherwise stores cleaned URL
VK: clip/video IDs when present, otherwise stores cleaned URL
```

Tracking query parameters such as `utm_*`, `igsh*`, `si`, `fbclid`, and `gclid` are removed.
