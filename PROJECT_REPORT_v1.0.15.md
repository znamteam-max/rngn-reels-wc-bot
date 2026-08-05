# PROJECT REPORT v1.0.15

Дата: 2026-08-05  
Проект: `@rngn_reels_wc_bot`  
Repository: `znamteam-max/rngn-reels-wc-bot`  
Production alias: `https://project-dcd2y.vercel.app`

## Версия и deployment

- Версия приложения: `1.0.15`.
- Implementation commit: `c3a423fb6a6d53077ca94e22db2af555e04fcda3`.
- Расширенные async regression tests: `3f96a5c`.
- Production deployment для implementation commit: `7zHfTrwD7QGTqCXANZfcFGGSZPcJ`.
- Vercel deployment page: `https://vercel.com/rngn2/project-dcd2y/7zHfTrwD7QGTqCXANZfcFGGSZPcJ`.
- Production health подтвердил commit `c3a423fb...`, version `1.0.15`, HTTP 200.

## Что реализовано

- Webhook больше не запускает runtime DDL. При старой схеме он безопасно возвращает HTTP 503.
- Добавлен durable dedupe входящих Telegram updates: `telegram_updates` со статусами `processing/done/failed`, hash payload, reclaim failed/stale update после 5 минут.
- Добавлена очередь `background_jobs`, partial unique dedupe index, allowlist из девяти job kinds, retries, `retry_after`, dead jobs и stale recovery.
- Добавлен worker `api/cron/process-jobs.py`: `FOR UPDATE SKIP LOCKED`, максимум 20 jobs, 20 секунд, 10 Telegram sends и 10 Sheets videos.
- Добавлен защищённый migration endpoint `api/admin/migrate.py`.
- Добавлены `bulk_operations` и chunked `/return_missing_dates` по 10 видео без массовой работы внутри webhook.
- Approval и `/add_znambo` сохраняют `sheet_sync_status=queued`; Google Sheets выполняется только worker-ом.
- Sheets video jobs группируются до 10: один `batchGet`, один `batchClear` при необходимости и один `batchUpdate`; Google service переиспользуется в invocation.
- Dashboard и FIFO pump coalesce по `dashboard:main` и `queue:pump:main`.
- Daily report, YouTube metrics, full Sheets resync и non-critical Telegram notifications переведены в jobs.
- Добавлены `/jobs_status` и подтверждаемый superadmin-only `/retry_failed_jobs`.
- Добавлен lazy `ConnectionPool(min_size=0, max_size=4, timeout=5, max_idle=60)`.
- `psycopg_pool 3.3.1` vendored с лицензией: Vercel builder не включал distribution в Python runtime даже при корректной dependency.
- `TelegramAPIError` хранит `status_code`, `description`, `retry_after`.
- Health read-only показывает schema, pool, jobs, Sheets, Telegram updates, bulk и webhook timing без секретов.

## Миграция и база

- Migration rollout deployment: `2ky1Edm9voXLL5kQtyEqeAv3RXcD`.
- Additive migration выполнена до финального read-only rollout.
- Production `schema_version`: `1.0.15`.
- Таблицы `telegram_updates`, `background_jobs`, `bulk_operations`, `schema_versions` доступны: health-запросы к ним успешны.
- Колонки Sheets sync доступны: production health вернул `queued_videos=0`, `failed_videos=0`.
- Neon pooled endpoint detection: `true`.
- Pool: enabled `true`, max size `4`, timeout `5s`, max idle `60s`.

## Проверки

- Локально: `109 passed`, дополнительно `21 subtests`, время `0.42s`.
- 100 concurrent unique updates: все 100 claimed.
- 100 concurrent duplicate updates: 1 claimed, 99 `duplicate_processing`.
- Dashboard burst: 50 enqueue используют один dedupe key `dashboard:main`.
- Worker claim test проверяет `FOR UPDATE SKIP LOCKED` и cap 20.
- Sheets batch test проверяет единичные `batchGet`, `batchClear`, `batchUpdate` для двух видео.
- Sheets outage test: ошибка enqueue не выбрасывается в webhook; video остаётся с queued side-effect state.
- Telegram 429 test: `retry_after=37` сохраняется.
- Bulk отключён при `BACKGROUND_JOBS_ENABLED=false` точным безопасным сообщением.
- Production endpoints: webhook GET `200`; worker без Authorization `401`; migrate без Authorization `401`.
- `WORK_CHAT_ID` отсутствует в runtime code/config.

## Production snapshot

Снимок финального health на commit `c3a423f`:

```text
schema_version=1.0.15
background_jobs_enabled=true
jobs queued=1 processing=0 failed=0 dead=0 stale=0
sheets queued=0 failed=0
telegram_updates failed_last_hour=0 processing_stale=0
bulk_operations active=0
pending_video_count=11
active_queue_video_id=171
active_queue_message_id=336
dashboard_message_id=234
```

Queued job — controlled `dashboard_refresh`, созданный миграцией. Массовый возврат заявок не запускался.

## Scheduler и live-ограничения

- Vercel project использует Hobby cron limits. Deployment с `* * * * *` отклонён Vercel ссылкой на Cron Jobs pricing.
- Защищённый minute worker endpoint развёрнут и готов, но внешний scheduler не настроен: нет авторизованного cron-jobs.org аккаунта и локально нет правильного production `CRON_SECRET`.
- Временный Hobby-compatible cron был развёрнут для controlled job, но в окне наблюдения 06:01-06:02 UTC invocation не произошёл; успешный production worker invocation не подтверждён.
- Финальный `vercel.json` сохраняет два прежних daily cron: YouTube metrics `0 3 * * *` и daily report `0 6 * * *`.
- Controlled webhook p50/p95: production samples `0`; p50/p95 не измерены. Unit tests не выдаются за production acceleration proof.
- Реальная входящая Telegram interaction после rollout не подтверждена.
- Telegram `getWebhookInfo` и `max_connections=5` не применены/не проверены: правильный production bot token недоступен локально; `.env.local` относится к другому боту и намеренно не использован.
- Маленький live bulk на двух controlled rows не выполнялся: безопасных тестовых pending rows не было. Production mass return не запускался.
- Two-worker безопасность проверена контрактом `SKIP LOCKED` и unit tests, но параллельный production invocation не выполнен без scheduler credentials.

## Что нужно для полного live E2E

1. На Hobby подключить cron-jobs.org к `GET /api/cron/process-jobs` раз в минуту с `Authorization: Bearer <CRON_SECRET>` или перевести Vercel team на Pro и вернуть minute cron в `vercel.json`.
2. С правильным production token выполнить `scripts/setup_bot_ui.py` при `WEBHOOK_URL=https://project-dcd2y.vercel.app`; скрипт установит `max_connections=5`.
3. Отправить боту `/jobs_status`, дождаться worker и проверить health `last_done_at`, webhook samples/p50/p95 и Telegram `getWebhookInfo`.
4. Создать ровно две controlled pending заявки без даты и отдельно подтвердить маленький bulk test. Не использовать текущую production очередь для теста.

## Rollback

- Additive schema, jobs и sync-state колонки не удалять.
- Worker можно остановить через `BACKGROUND_JOBS_ENABLED=false`; sync bulk при этом не включается.
- Approved video не откатывается при ошибке Sheets/Telegram side effect.
- Для code rollback вернуть предыдущий production deployment, сохранив schema `1.0.15`.
