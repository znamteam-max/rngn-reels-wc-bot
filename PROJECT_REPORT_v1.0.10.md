# RNGN Reels WC Bot - Znambo Quick Flow Report

Version: `1.0.10`
Date: `2026-07-16`
Local Project: `C:/Users/znambo/Documents/RNGN`
GitHub Repo: `https://github.com/znamteam-max/rngn-reels-wc-bot`
Production URL: `https://project-dcd2y.vercel.app`
Webhook URL: `https://project-dcd2y.vercel.app/api/webhook`
Bot: `@rngn_reels_wc_bot`

## Implemented

- Added private superadmin-only `/add_znambo` quick flow.
- Added superadmin-only button: `⚡ Добавить мой ролик`.
- Quick flow steps:
  - `/add_znambo`
  - Instagram/Reels link
  - active duplicate check by normalized `instagram_id`
  - publish date with `Сегодня`, `Вчера`, `Позавчера`, `YYYY-MM-DD`, `DD.MM`, `D.M`
  - immediate approved DB row
  - immediate Google Sheets sync
- Quick flow auto-fills:
  - author: `Знамбо (@znambo)`
  - voice: `Знамбо (@znambo)`
  - montage: `Знамбо (@znambo)`
  - added_by: current superadmin
  - checked_by: current superadmin
  - status: `approved`
  - video_type: `regular`
- Added safe role-specific upsert/resolve for `Знамбо (@znambo)` in `author`, `montage`, and `voice`.
- Active duplicates stop with `Этот ролик уже есть в базе.` and show ID/status/date.
- Deleted duplicates are restored as `approved` after the date step, without inserting a new row.
- Restore clears stale admin-review fields and optional platform links.
- No pending review card is created.
- No work/common chat send is used.
- `/add_znambo` does not ask for author, voice, montage, YouTube, TikTok, VK, or admin review.
- Telegram command setup now keeps `/add_znambo` out of the global command list and adds it only in a chat scope for bootstrap superadmins.

## Files Changed

- `bot/handlers.py`
- `bot/public_patch.py`
- `bot/version.py`
- `pyproject.toml`
- `scripts/setup_bot_ui.py`
- `tests/test_links.py`
- `uv.lock`
- `PROJECT_REPORT_v1.0.10.md`

## Deployment

Deployment path:

```text
git push origin main
Vercel Git integration deployed production
py scripts/setup_bot_ui.py
```

Main code commit:

```text
a9630e21a3b3834d014adadc97c6b538b366fa9d Add Znambo quick video flow
```

Production health:

```text
GET https://project-dcd2y.vercel.app/api/health
ok: true
version: 1.0.10
missing_env: []
optional_missing_env: []
video_type_column: true
youtube_id_column: true
idx_videos_video_type: true
idx_videos_youtube_id: true
prokudin_active_rows: 1
```

Telegram commands:

```text
py scripts/setup_bot_ui.py
Telegram bot commands and menu button are configured.
Global command list: no /add_znambo
Bootstrap superadmin chat scope: add_znambo — Быстро добавить мой ролик
```

## Production Smoke

All production webhook calls returned `{"ok": true}`.

```text
POST /api/webhook /add_znambo -> ok
POST /api/webhook existing IG duplicate https://www.instagram.com/reel/DaLt0gSMxBq/ -> ok
POST /api/webhook /start -> ok
```

Duplicate-test result:

```text
Google Sheets candidate: video ID 9
status: approved
instagram_url: https://www.instagram.com/reel/DaLt0gSMxBq/
Expected behavior: active duplicate stops the flow and clears session.
No new DB video row was intentionally submitted for this smoke.
```

Google Sheets result:

```text
Videos sheet header read: ok
Required columns present: id, status, video_type, instagram_url, instagram_id
No-op write to Videos!A1:V1: ok
Columns: 22
```

## Confirmations

- `/new_video` still starts the regular Instagram-first flow.
- `/new_bigrecap` still starts the YouTube-first big recap flow.
- `/add_znambo` is superadmin-only.
- Normal users do not see the `⚡ Добавить мой ролик` button.
- `/add_znambo` invalid Instagram input keeps the session active.
- `/add_znambo` success card contains no empty YouTube/TikTok/VK lines.
- `WORK_CHAT_ID` was not reintroduced in runtime code (`bot`, `scripts`, `api`).
- No metrics, payments, or extra platform-link work was added to `/add_znambo`.

## Verification

Local checks:

```text
py -m unittest tests.test_links -v
py -m compileall bot scripts tests
git diff --check
rg -n "WORK_CHAT_ID" bot scripts api --glob "*.py"
```

Result:

```text
34 tests passed
compileall passed
git diff --check passed
WORK_CHAT_ID runtime search: no matches
```

Note:

```text
py -m pytest -q was not available locally because pytest is not installed.
The project tests are unittest-based and passed through py -m unittest.
```
