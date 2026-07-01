# RNGN Reels WC Bot - YouTube Metrics Module Report

Version: `1.0.7`
Date: `2026-07-01`
Local Project: `C:/Users/znambo/Documents/RNGN`
GitHub Repo: `https://github.com/znamteam-max/rngn-reels-wc-bot`
Production URL: `https://project-dcd2y.vercel.app`
Webhook URL: `https://project-dcd2y.vercel.app/api/webhook`
Bot: `@rngn_reels_wc_bot`

## Code And Deploy

Main product commit:

```text
c317d54 Add YouTube metrics sync
```

Final clean Vercel deployment:

```text
Deployment ID: dpl_7vUwMLkSDYDBmiTQCM8MrXir7TPB
Status: READY
Deployment URL: https://project-dcd2y-cel444enb-rngn2.vercel.app
Alias: https://project-dcd2y.vercel.app
Functions:
- api/health
- api/webhook
- api/cron/youtube-metrics
```

Temporary protected runtime endpoints were used only for DB migration and verification, then removed. Final production check confirms:

```text
GET /api/runtime_admin -> 404
```

## Implemented

- Added `YOUTUBE_API_KEY` support as optional metrics env.
- Added `CRON_SECRET` support for protected cron sync.
- Health/webhook GET now include `optional_missing_env` without making metrics env fatal.
- Added `video_metrics_snapshots` table for daily metrics snapshots.
- Added latest YouTube columns on `videos`:
  - `youtube_views`
  - `youtube_likes`
  - `youtube_comments`
  - `youtube_last_sync_at`
- Added YouTube API client using Data API v3 `videos?part=statistics`.
- Reused/covered existing YouTube URL normalization for `youtu.be`, `watch?v=`, and `shorts/`.
- Added admin commands:
  - `/sync_youtube_metrics`
  - `/metrics_youtube_today`
  - `/metrics_youtube_all`
  - `/metrics_video <video_id>`
- Added `MetricsRaw` Google Sheets append sync.
- Added protected cron endpoint:
  - `GET /api/cron/youtube-metrics`
  - requires `Authorization: Bearer <CRON_SECRET>`
- Added Vercel Cron schedule:
  - `0 3 * * *`
- Updated Telegram command menu to 12 commands.

## Env Status

Vercel production env:

```text
YOUTUBE_API_KEY: present
CRON_SECRET: present
```

No secret values were printed or committed.

Final health check:

```text
GET /api/health
ok: true
missing_env: []
optional_missing_env: []
```

## Database Migration

Production DB migration completed successfully.

Verified:

```text
video_metrics_snapshots table: exists
videos latest columns:
- youtube_comments
- youtube_last_sync_at
- youtube_likes
- youtube_views
```

Approved videos with YouTube URLs:

```text
1
```

## Sync Result

Manual `/sync_youtube_metrics` was invoked through the production webhook:

```text
webhook response: {"ok": true}
```

Authorized cron endpoint was also invoked successfully after `CRON_SECRET` setup:

```text
ok: true
missing_key: false
no_videos: false
total_videos: 1
success_count: 1
error_count: 0
total_views: 188736
total_likes: 908
total_comments: 9
top_video_id: 9
top_views: 188736
sheet_status: ok
sheet_appended: 0
sheet_error: null
```

`sheet_appended: 0` on the authorized cron run is expected because the earlier manual sync already appended today's row to `MetricsRaw`; duplicate daily writes are skipped.

Current DB metrics state:

```text
youtube_snapshot_count: 1
snapshot_statuses: {"ok": 1}
latest_metric_videos: 1
latest_views: 188736
latest_likes: 908
latest_comments: 9
```

## MetricsRaw Status

First manual sync:

```text
sheet_status: ok
sheet_appended: 1
```

Repeat cron sync same day:

```text
sheet_status: ok
sheet_appended: 0
```

This confirms `MetricsRaw` is updated and same-day duplicates are skipped.

## `/metrics_youtube_all` Result

The formatter used by the bot returns:

```text
YouTube всего

Видео с метриками: 1
Просмотры: 188 736
Лайки: 908
Комментарии: 9

Топ-5 по просмотрам:
1. ID 9 — 188 736
```

`/metrics_youtube_all` and `/metrics_video 9` were invoked through the production webhook and returned `{"ok": true}` from the webhook handler.

## Cron Endpoint Status

Unauthenticated request:

```text
GET /api/cron/youtube-metrics -> 401
```

Authorized request with `Authorization: Bearer <CRON_SECRET>`:

```text
GET /api/cron/youtube-metrics -> 200
ok: true
```

Vercel Cron is configured in `vercel.json`:

```json
{
  "path": "/api/cron/youtube-metrics",
  "schedule": "0 3 * * *"
}
```

## Telegram / Runtime Checks

Telegram webhook:

```text
getWebhookInfo.ok: true
url: https://project-dcd2y.vercel.app/api/webhook
pending_update_count: 0
```

Telegram commands:

```text
getMyCommands count: 12
```

Runtime checks:

```text
GET /api/health -> ok=true
GET /api/webhook -> ok=true
GET /api/runtime_admin -> 404
GET /api/cron/youtube-metrics without auth -> 401
```

## Tests

Local verification:

```text
py -B -m unittest discover -s tests
py -B -m compileall -q api bot scripts tests
git diff --check
```

Result:

```text
22 tests passed
compileall passed
git diff --check passed
```

Test coverage added for:

- YouTube ID extraction:
  - `youtu.be`
  - `watch?v=`
  - `shorts/`
  - extra query params
  - invalid URL
- YouTube API response parsing:
  - `viewCount`
  - `likeCount`
  - `commentCount`
  - missing `likeCount`
  - missing `commentCount`
  - API item not found
- Summary calculations:
  - sum views/likes/comments
  - top video by views
  - latest snapshot per video
- Command permission:
  - non-admin denied for `/sync_youtube_metrics`
  - admin can run `/sync_youtube_metrics`

## Runtime Logs Summary

The latest `youtube_metrics_sync` DB log contains:

```text
ok: true
missing_key: false
total_videos: 1
success_count: 1
error_count: 0
sheet_status: ok
sheet_error: null
```

No YouTube metric sync errors were recorded in the final sync result.

## Remaining Blockers

No blocker for the YouTube MVP.

Intentional scope exclusions remain:

```text
Instagram metrics: not started until Meta access/SMS verification is complete
TikTok metrics: not started
VK metrics: not started
```
