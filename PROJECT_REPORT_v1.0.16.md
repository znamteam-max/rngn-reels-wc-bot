# PROJECT REPORT v1.0.16

Дата: 2026-08-05  
Проект: `@rngn_reels_wc_bot`  
Repository: `znamteam-max/rngn-reels-wc-bot`  
Production: `https://project-dcd2y.vercel.app`

## Итоговый статус

Версия `1.0.16` реализована, протестирована, отправлена в `main`, развёрнута в production и мигрирована. Ручной GitHub Actions E2E с OIDC успешно обработал накопившийся production job и записал heartbeat с `source=github_actions`.

Полный live E2E пока не заявляется: в окне наблюдения GitHub не создал первый run с событием `schedule`, из Telegram не вызывался `/worker_status`, а реальный approval с последующим Sheets sync намеренно не выполнялся без выбора конкретной production-заявки пользователем.

## Версия и deployment

- Версия приложения и схемы: `1.0.16`.
- Implementation commit: `4b055ad86c42e0fc456ef0f42a56fa519a8e52ee`.
- Migration rollout commit: `71410e3af7e08f853b05f0f4bf4aed117dfc5b3b`.
- Final read-only health commit: `303371bdc17764d4fd0e3658b44a7dacace32680`.
- Production deployment URL: `https://project-dcd2y-bjyf2g14o-rngn2.vercel.app`.
- GitHub production deployment record: `5757233729`, status `success`.
- Production alias подтвердил HTTP 200, commit `303371b...`, version `1.0.16`, schema `1.0.16`.
- Внутренний Vercel deployment ID не получен: Vercel connector и CLI отвечают `403/Not authorized` для scope `rngn2`. Уникальный deployment URL и GitHub deployment record зафиксированы выше.

## Что реализовано

- GitHub Actions scheduler `.github/workflows/process-background-jobs.yml`: cron `*/5 * * * *`, `workflow_dispatch`, OIDC permission, concurrency и bounded drain до 12 вызовов.
- Проверка GitHub OIDC по официальным issuer/JWKS, audience, repository, owner, `refs/heads/main` и разрешённым event names.
- Worker сохранил поддержку `CRON_SECRET`; endpoint без авторизации в production возвращает HTTP 401.
- Добавлены job priorities, `processed_by_kind` и durable `worker_heartbeats`.
- Health показывает heartbeat, возраст последнего успеха и предупреждение только при stale worker с непустой очередью.
- Добавлены admin-only `/worker_status` и superadmin-only `/run_jobs_now` без выполнения jobs внутри Telegram webhook.
- Live dashboard использует debounce 3 секунды, PostgreSQL advisory lock, короткий Telegram timeout и coalesced repair job.
- Live FIFO pump отправляет максимум одну карточку, сохраняет stale protection и ставит repair job при сетевой ошибке.
- `WORK_CHAT_ID` в runtime-код и config не возвращался.

## Миграция

- До rollout: `schema_version=1.0.15`, jobs `queued=1`, heartbeat отсутствовал.
- Additive migration выполнена временным идемпотентным rollout-вызовом `ensure_runtime_migrations(force=True)` через production health.
- После проверки временный вызов удалён; финальный health снова read-only.
- После rollout: `schema_version=1.0.16`.
- Таблица `worker_heartbeats` работоспособна: production worker создал и обновил строку heartbeat.
- Существующий `background_jobs` и пользовательские данные не удалялись.

## GitHub worker E2E

Workflow зарегистрирован на GitHub как `327595708`, state `active`.

### Ручной запуск

- Run ID: `30983495056`.
- URL: `https://github.com/znamteam-max/rngn-reels-wc-bot/actions/runs/30983495056`.
- Event: `workflow_dispatch`.
- Commit: `303371bdc17764d4fd0e3658b44a7dacace32680`.
- Result: `success`.
- Job ID: `92232927733`, шаг `Drain production queue` завершён успешно.
- Время run: `2026-08-05T07:02:38Z` - `2026-08-05T07:02:48Z`.

Jobs до drain:

```text
queued=1 processing=0 failed=0 dead=0
kind=dashboard_refresh
```

Jobs после drain:

```text
queued=0 processing=0 failed=0 dead=0 stale_processing=0
last_done_at=2026-08-05T07:02:46.205154+00:00
```

Первый heartbeat:

```text
last_started_at=2026-08-05T07:02:45.814192+00:00
last_success_at=2026-08-05T07:02:46.222871+00:00
last_claimed=1
last_done=1
last_remaining=0
source=github_actions
invocation_id=6a98f0b8-e481-48a7-81ab-37d224d1555b
healthy=true
```

### Scheduled запуск

- Наблюдение: от создания workflow `2026-08-05T06:55:11Z` до `2026-08-05T07:19:00Z`.
- Run с `event=schedule`: не создан в этом окне.
- Scheduled run ID: отсутствует.
- Второй независимый heartbeat: не подтверждён.
- Workflow остаётся `active`, файл находится в default branch `main`, cron соответствует заданию.
- GitHub официально предупреждает, что scheduled events могут задерживаться или пропускаться при высокой нагрузке, особенно около начала часа.

Scheduler нельзя считать полностью подтверждённым до появления минимум одного успешного run с `event=schedule` и нового `invocation_id` в health.

## Dashboard, FIFO и Telegram

- Реальное worker-взаимодействие с Telegram подтверждено: job обновил production dashboard `message_id=234`, `dashboard_updated_at=2026-08-05T07:02:45.873394+00:00`.
- Очередь заявок после обновления сохранена: pending `11`, active video `171`, active message `336`.
- Dashboard burst: тест на 50 enqueue использует один dedupe key `dashboard:main`; v1.0.16 tests отдельно покрывают debounce, advisory-lock contention, первый live refresh и repair после ошибки.
- FIFO pump: тестами подтверждены no-op при valid active card, short-timeout path и repair enqueue при ошибке. Массовая отправка backlog не добавлена.
- Входящая Telegram interaction после rollout не выполнялась; production health показывает `failed_last_hour=0`, `processing_stale=0`, но timing samples `0`.
- `/worker_status` в реальном Telegram не вызывался. Формат, admin guard и stale/healthy логика покрыты тестами.
- Прямой `getWebhookInfo` не вызывался: корректный production bot token локально недоступен, а `.env.local` относится к другому боту и не использовался.

## Sheets worker sync

- Финальный snapshot: `queued_videos=0`, `failed_videos=0`.
- Реальный approval -> queued `sheets_sync_video` -> worker sync в этой выкладке не выполнялся.
- Причина: в production есть реальные pending-заявки, и агент не выбирал заявку для бизнес-approval без явного решения пользователя.
- Async enqueue, batching, retry и отсутствие тяжёлого Sheets-вызова в webhook подтверждены regression tests v1.0.15/v1.0.16.

## Тесты и сборка

- Python: `129 tests passed`.
- Node: `16 tests passed`.
- Python `compileall`: успешно.
- Ruff critical rules `E9,F63,F7,F82`: успешно.
- `git diff --check`: успешно.
- GitHub Actions bash body: `bash -n` успешно.
- `npx vercel build --prod`: успешно после добавления `.venv/Scripts` в PATH локального build process.
- Production bundle содержит `jwt`, `cryptography` и `psycopg_pool`.
- OIDC tests: valid token, wrong repo/ref/audience, expired и unsigned token, сохранение `CRON_SECRET` auth.
- Worker tests: heartbeat, stale warning, empty-queue health, priorities, `processed_by_kind`, max calls и token masking.

## Финальный production snapshot

```text
version=1.0.16
schema_version=1.0.16
commit_sha=303371bdc17764d4fd0e3658b44a7dacace32680
missing_env=[]
optional_missing_env=[]
jobs queued=0 processing=0 failed=0 dead=0 stale=0
worker source=github_actions claimed=1 done=1 remaining=0 healthy=true
sheets queued=0 failed=0
telegram_updates failed_last_hour=0 processing_stale=0
pending_video_count=11
active_queue_video_id=171
active_queue_message_id=336
dashboard_message_id=234
```

## Ограничения live E2E

Для полного production sign-off остаются три наблюдаемых шага:

1. Дождаться успешного GitHub run с `event=schedule` и подтвердить второй heartbeat.
2. Отправить боту `/worker_status` от реального admin и проверить ответ.
3. Выбрать одну реальную pending-заявку, выполнить обычный approval и подтвердить `sheets_sync_video` как done worker-ом.

Массовый `/return_missing_dates` не запускался. `WORK_CHAT_ID` отсутствует в runtime. Секреты и raw OIDC token в отчёт и логи не выводились.

## Rollback

- Additive таблицу `worker_heartbeats` и строку schema `1.0.16` не удалять.
- Scheduler можно остановить через GitHub Actions disable без отката пользовательских данных.
- При code rollback вернуть предыдущий production deployment, сохранив additive schema `1.0.16`.
