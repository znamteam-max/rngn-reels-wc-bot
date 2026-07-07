# RNGN Reels WC Bot - Big Recap Report

Version: `1.0.8`
Date: `2026-07-07`
Local Project: `C:/Users/znambo/Documents/RNGN`
GitHub Repo: `https://github.com/znamteam-max/rngn-reels-wc-bot`
Production URL: `https://project-dcd2y.vercel.app`
Webhook URL: `https://project-dcd2y.vercel.app/api/webhook`
Bot: `@rngn_reels_wc_bot`

## Implemented

- Added separate submission type `bigrecap` for "большой рекап".
- Added `/new_bigrecap` and menu button `🧵 Добавить большой рекап`.
- Normal `/new_video` submissions now explicitly store `video_type = regular`.
- Preview, admin cards, final cards, and duplicate cards show `Тип: большой рекап` only for big recap rows.
- Added `videos.video_type text NOT NULL DEFAULT 'regular'` and `idx_videos_video_type`.
- Preserved `video_type` through insert, revision update, admin `/edit_video`, preview edit, and deleted-duplicate restore in `bot/public_patch.py`.
- Added Google Sheets `Videos.video_type` column handling. If missing, it is inserted safely after `status`.
- Added live author seed: `Прокудин (@ny_pochemu)`.
- Replaced wording with `Была ли в ролике озвучка другого автора?`.
- Replaced voice "no" button with `Нет, не было`.
- Added runtime migration on Vercel cold start so production applies schema and Prokudin seed without keeping a temporary admin endpoint.

## Files Changed

- `api/health.py`
- `api/webhook.py`
- `bot/handlers.py`
- `bot/messages.py`
- `bot/public_patch.py`
- `bot/runtime_migrations.py`
- `bot/sheets.py`
- `bot/version.py`
- `scripts/init_db.py`
- `scripts/setup_bot_ui.py`
- `people.live-seed.csv`
- `tests/test_links.py`
- `README.md`
- `pyproject.toml`
- `uv.lock`

## Deployment

Deployment path:

```text
git push origin main
Vercel Git integration deployed production
```

Final production health:

```text
GET https://project-dcd2y.vercel.app/api/health
ok: true
version: 1.0.8
missing_env: []
optional_missing_env: []
video_type_column: true
video_type_nullable: NO
video_type_default: 'regular'::text
idx_videos_video_type: true
prokudin_active_rows: 1
```

Temporary runtime admin endpoint status:

```text
GET /api/runtime_admin -> 404
```

Telegram commands:

```text
getMyCommands ok: true
count: 13
has_new_bigrecap: true
```

## Manual Production Test

Webhook e2e smoke test on final deploy:

```text
POST /api/webhook with /new_bigrecap -> {"ok": true}
POST /api/webhook with /cancel -> {"ok": true}
```

No fake video was submitted, so no test request was left in the admin queue or Google Sheet.

## Verification

Local checks:

```text
py -B -m compileall api bot scripts
py -B -m unittest discover -s tests -v
git diff --check
```

Result:

```text
27 tests passed
compileall passed
git diff --check passed
```

Old wording check:

```text
No matches for: Нужна дополнительная озвучка
No matches for: Да, нужна
New wording present: Была ли в ролике озвучка другого автора?
```

## Explicitly Not Added

- Instagram metrics
- New YouTube metrics work
- Payments
- New `WORK_CHAT_ID` dependency
