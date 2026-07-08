# RNGN Reels WC Bot - Big Recap YouTube-First Report

Version: `1.0.9`
Date: `2026-07-08`
Local Project: `C:/Users/znambo/Documents/RNGN`
GitHub Repo: `https://github.com/znamteam-max/rngn-reels-wc-bot`
Production URL: `https://project-dcd2y.vercel.app`
Webhook URL: `https://project-dcd2y.vercel.app/api/webhook`
Bot: `@rngn_reels_wc_bot`

## Implemented

- Changed only the big recap submission flow.
- `/new_video` remains Instagram-first:
  - Instagram/Reels
  - author
  - voice
  - montage
  - optional YouTube/TikTok/VK
- `/new_bigrecap` now starts from required YouTube:
  - YouTube
  - author
  - voice
  - montage
  - optional VK
- Big recap no longer asks for Instagram or TikTok.
- Big recap rows keep `instagram_url`, `instagram_id`, `tiktok_url`, and `tiktok_id` as real `NULL` values.
- Added `extract_youtube_id(url)` helper and duplicate detection by `youtube_id`.
- Active duplicate YouTube big recaps are blocked.
- Deleted duplicate big recaps can be restored as `pending` through the updated `bot/public_patch.py` restore path.
- Added `idx_videos_youtube_id` migration.
- Added `youtube_id` to `Videos` sheet rows and safe header handling.
- Big recap cards hide empty Instagram/TikTok lines and show only YouTube/VK platform lines.
- Voice prompt keeps the new wording and now exposes explicit `Да, была` / `Нет, не было` buttons.

## Files Changed

- `bot/handlers.py`
- `bot/links.py`
- `bot/messages.py`
- `bot/public_patch.py`
- `bot/runtime_migrations.py`
- `bot/sheets.py`
- `bot/version.py`
- `scripts/init_db.py`
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

Main code commit:

```text
7b2fef8 Start big recap submissions from YouTube
```

Final production health:

```text
GET https://project-dcd2y.vercel.app/api/health
ok: true
version: 1.0.9
video_type_column: true
youtube_id_column: true
idx_videos_video_type: true
idx_videos_youtube_id: true
prokudin_active_rows: 1
missing_env: []
optional_missing_env: []
```

Telegram commands:

```text
getMyCommands ok: true
count: 13
has_new_bigrecap: true
```

## Production Smoke Tests

All webhook calls returned `{"ok": true}`.

Regular flow smoke:

```text
POST /api/webhook /new_video -> ok
POST /api/webhook /cancel -> ok
```

Big recap invalid YouTube smoke:

```text
POST /api/webhook /new_bigrecap -> ok
POST /api/webhook Instagram URL as first bigrecap link -> ok
POST /api/webhook /cancel -> ok
```

Big recap valid YouTube smoke:

```text
POST /api/webhook /new_bigrecap -> ok
POST /api/webhook https://youtu.be/codexv109smoke -> ok
POST /api/webhook /cancel -> ok
```

No fake request was submitted to admin review and no test row was written to Google Sheets.

## Confirmations

- Big recap starts from YouTube, not Instagram.
- Big recap invalid first-step Instagram/VK/random input stays on the YouTube step with the new invalid YouTube message.
- Big recap proceeds to author / voice / montage after valid YouTube.
- Big recap asks VK after montage.
- Big recap does not ask TikTok.
- Big recap cards do not show empty Instagram/TikTok lines.
- `WORK_CHAT_ID` was not reintroduced as a required runtime dependency.
- No Instagram metrics, new YouTube metrics work, payments, or `WORK_CHAT_ID` work were added.

## Verification

Local checks:

```text
py -B -m compileall api bot scripts
py -B -m unittest discover -s tests -v
git diff --check
```

Result:

```text
33 tests passed
compileall passed
git diff --check passed
```

Old wording check:

```text
No matches for: Нужна дополнительная озвучка
```
