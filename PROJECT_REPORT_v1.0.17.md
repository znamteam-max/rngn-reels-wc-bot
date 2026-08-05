# PROJECT REPORT v1.0.17

Дата: 2026-08-05  
Проект: `@rngn_reels_wc_bot`  
Repository: `znamteam-max/rngn-reels-wc-bot`  
Production: `https://project-dcd2y.vercel.app`

## Итоговый статус

Версия `1.0.17` реализована, протестирована, отправлена в `main`, развёрнута в production и мигрирована. Немедленный запуск ready-задач больше не зависит от GitHub scheduler: контролируемый production `dashboard_refresh` был принят event-driven kicker, выполнен отдельным Python worker invocation и записал heartbeat `source=event_kick`. В этот период GitHub Actions не запускался.

GitHub Actions сохранён как резервный и ручной drain. Долгие отложенные retries по-прежнему остаются durable и могут быть подняты следующим Telegram update, `/kick_worker` или резервным GitHub worker.

## Коммиты и deployment

- Implementation commit: `e47e20cb35891cfc47c1830c089344bf0041acc0` (`feat: add event-driven background worker kick`).
- Migration/E2E rollout commit: `ade1eb40b92d2a61860f250c83e4c1443b45b302`.
- Final read-only health commit: `038f8f0ba46b2094a1d753b607f5fa8903faa495`.
- Implementation deployment: `https://project-dcd2y-iqv84knrl-rngn2.vercel.app`, GitHub deployment record `5758269938`, `success`.
- Migration/E2E deployment: `https://project-dcd2y-h53mmsupe-rngn2.vercel.app`, GitHub deployment record `5758319273`, `success`.
- Финальный проверенный runtime deployment: `https://project-dcd2y-m23p7903w-rngn2.vercel.app`, GitHub deployment record `5758386810`, `success`.
- Production alias после cleanup: HTTP 200, commit `038f8f0...`, version/schema `1.0.17`.
- Внутренний Vercel deployment ID не получен: Vercel connector/CLI для scope `rngn2` отвечал `403/Not authorized`. Уникальные deployment URL и GitHub deployment records зафиксированы выше.

## Что реализовано

- Добавлен Node Web Handler `api/internal/kick-worker.js`: POST + Bearer auth, constant-time secret check, немедленный HTTP 202 и bounded drain через `context.waitUntil(...)`.
- Kicker вызывает только фиксированные same-origin endpoints, не принимает target URL, job kind или произвольную команду.
- Drain ограничен 6 worker calls, 50 секундами, 25-секундным timeout на call, двумя retry для 429/5xx и chain depth `2`.
- После обработки выполняется один settle pass; near-future job ожидается только в пределах оставшегося бюджета.
- Добавлены PostgreSQL lease/coalescing в `worker_kick_state` и Python API `kick_worker_if_ready(...)` с короткими timeout `0.5/1.5` секунды.
- Добавлен защищённый completion endpoint, который очищает lease и транзакционно проверяет ready jobs, закрывая shutdown race.
- Kick запускается только после commit для owned enqueue. Внутри пользовательской транзакции сетевой вызов не выполняется.
- Успешный Telegram update делает безопасный `webhook_tail` kick; ошибка kicker не отменяет бизнес-операцию и не превращает webhook в failure.
- Worker response дополнен `remaining_ready`, `remaining_queued_total` и `next_available_in_seconds`.
- Trusted headers записывают `source=event_kick` только после корректной `CRON_SECRET` авторизации и проверки User-Agent.
- Health и `/worker_status` разделяют ready/future jobs и показывают состояние event kick; добавлена superadmin-команда `/kick_worker`.
- GitHub workflow оставлен active, concurrency сохранён, cron смещён на `2-57/5 * * * *`.
- Версия приложения, schema marker, `pyproject.toml` и `uv.lock` обновлены до `1.0.17`.

## Миграция

- До rollout production показывал `version=1.0.17`, `schema_version=1.0.16`.
- Additive migration запущена одноразовым идемпотентным rollout-вызовом `ensure_runtime_migrations(force=True)`.
- Создана таблица `worker_kick_state`, добавлена строка `worker_name=main`, записан schema marker `1.0.17`.
- После E2E одноразовый код удалён; два последовательных GET health не изменили kick counters.
- Финальное состояние: `version=1.0.17`, `schema_version=1.0.17`, rollout-полей в health нет.
- Существующие jobs и пользовательские данные не удалялись.

## Mixed Node/Python runtime

Локальный `npx vercel build --prod` успешно собрал обе функции одного Vercel project:

```text
api/internal/kick-worker.func
runtime=nodejs24.x
maxDuration=60

api/internal/complete-worker-kick.func
runtime=python3.12
maxDuration=300
```

Production Git deployment прошёл со статусом `success`. Webhook URL не менялся: `https://project-dcd2y.vercel.app/api/webhook`.

## Security smoke

Проверено на финальном production deployment:

```text
GET  /api/internal/kick-worker                   -> 405
POST /api/internal/kick-worker без auth          -> 401
POST /api/internal/complete-worker-kick без auth -> 401
```

Request body ограничен 4096 байт. Секрет, payload jobs, target URL, stack trace и drain UUID не возвращаются health endpoint и не попадают в прикладные safe logs.

## Production event-kick E2E

Контролируемый job: coalesced `dashboard_refresh`, созданный миграционным repair enqueue.

До drain:

```text
queued=1 ready=1 future=0 processing=0 failed=0 dead=0
```

Первый accepted kick:

```text
last_requested_at=2026-08-05T08:27:53.639327+00:00
last_accepted_at=2026-08-05T08:27:53.785335+00:00
kicker response=accepted
ready_jobs=1
```

Результат:

```text
dashboard_updated_at=2026-08-05T08:27:53.976425+00:00
job last_done_at=2026-08-05T08:27:54.310711+00:00
heartbeat source=event_kick
heartbeat last_success_at=2026-08-05T08:27:59.808349+00:00
kick last_completed_at=2026-08-05T08:28:00.914916+00:00
queued=0 ready=0 future=0 processing=0 failed=0 dead=0
```

Последняя heartbeat-строка показывает `claimed=0`, `done=0`, потому что предусмотренный settle pass после уже выполненного job сделал ещё один пустой worker call и обновил singleton heartbeat. Завершение job подтверждается более ранним `last_done_at`, обновлением dashboard, accepted kick и тем же drain-окном `source=event_kick`.

## Доказательство отсутствия GitHub worker

- Event-kick job выполнен около `08:27:54 UTC`.
- GitHub runs были запрошены до и после E2E.
- Единственный run workflow: ID `30983495056`, event `workflow_dispatch`, `07:02:38-07:02:48 UTC`, commit `303371b...`.
- Между ручным run в `07:02 UTC` и event-kick E2E в `08:27 UTC` нового workflow run не было.
- Следовательно, контролируемый `dashboard_refresh` не мог быть обработан GitHub Actions.
- Workflow ID `327595708` остаётся `active`; run с `event=schedule` по-прежнему не наблюдался и не заявляется как подтверждённый.

Эти данные подтверждают scheduler-independent запуск для ready job в production. Они не отменяют резервный GitHub drain и не обещают немедленный запуск jobs с задержкой, выходящей за 50-секундный kicker budget.

## Load и regression

- 50 конкурентных kick attempts: один lease owner, один HTTP POST, остальные запросы coalesced.
- Dashboard burst: 50 enqueue используют один dedupe key `dashboard:main`.
- 100 Telegram update claims: 100 уникальных mutations; 100 одновременных повторов одного update дали `1 claimed + 99 duplicate_processing`.
- 20 Sheets jobs сохранили 20 уникальных ключей `sheets:video:<id>` и не делали network kick внутри открытой business transaction.
- Worker claim использует PostgreSQL `FOR UPDATE SKIP LOCKED` и ограничивает batch 20 jobs.
- Таймаут kicker истекает безопасно: lease освобождается, durable job остаётся, бизнес-операция не падает.

Контролируемый локальный benchmark 100 вызовов webhook-tail wrapper с mocked kicker failure:

```text
samples=100
p50=0.0034 ms
p95=0.0053 ms
max=0.0185 ms
target p95 < 3000 ms
```

Это измерение только защитного webhook-tail wrapper с mock, а не сетевой production latency. Production health в момент проверки имел `samples=0`, поэтому production p50/p95 не заявляются.

## Тесты и build

- Python: `143 passed, 21 subtests passed`.
- Node: `24 passed`.
- Новые v1.0.17 Python tests: `14 passed`.
- Python `compileall`: успешно.
- Ruff critical rules `E9,F63,F7,F82`: успешно.
- `git diff --check`: успешно.
- `npm run verify`: успешно.
- `npx vercel build --prod`: успешно.
- Kicker tests покрывают method/auth guards, immediate 202, `waitUntil`, local active drain, batch/settle/retry limits, invalid JSON, 401 stop, fixed target и chain cap.

## Финальный production snapshot

```text
version=1.0.17
schema_version=1.0.17
commit_sha=038f8f0ba46b2094a1d753b607f5fa8903faa495
jobs queued=0 ready=0 future=0 processing=0 failed=0 dead=0 stale_processing=0
worker healthy=true state=idle source=event_kick
worker_kick lease_active=false request_count=2 accepted_count=1 skipped_lease_count=0
worker_kick last_error=null
sheets queued_videos=0 failed_videos=0
webhook_performance samples=0 p50=null p95=null
```

`request_count=2` включает accepted rollout kick и последующую одноразовую health-проверку, которая обнаружила уже пустую очередь. Cleanup удалил этот rollout side effect; два финальных GET сохранили counters без изменений.

## Sheets и бизнес-безопасность

- Реальный approval production-видео намеренно не выполнялся: в очереди находятся пользовательские заявки, а безопасный тестовый video ID не был предоставлен.
- Поэтому цепочка `approval -> sheet_sync_status=queued -> event kick -> synced` не заявляется как live-подтверждённая.
- Финальный snapshot Sheets чистый: `queued_videos=0`, `failed_videos=0`.
- Async enqueue, уникальность Sheets jobs, batching и отсутствие тяжёлого Sheets-вызова внутри webhook покрыты тестами.

## Ограничения

- Первый GitHub run с `event=schedule` всё ещё отсутствует; GitHub cron остаётся включённым резервом, но scheduled execution не объявляется подтверждённым.
- Vercel connector и локальный CLI не дали доступ к runtime logs/internal deployment ID для team scope. Production состояние подтверждено GitHub deployment records и публичным health.
- Реальный Telegram `/kick_worker` не отправлялся: production E2E использовал контролируемый rollout hot path с тем же `kick_worker_if_ready(...)` и после проверки был удалён.
- Реальный approval/Sheets sync и production webhook latency sample не выполнялись по причинам выше.
- Settle pass обновляет singleton heartbeat пустым последним invocation; источник остаётся точным, но итоговые `last_claimed/last_done` не являются агрегатом всего drain.

## Safety confirmation

- Массовый `/return_missing_dates` не запускался.
- `WORK_CHAT_ID` отсутствует в runtime-коде и config (`bot`, `api`, `scripts`).
- Production bot token из локального `.env.local` не использовался: этот файл относится к другому боту.
- Секреты и job payloads в отчёт, git и диагностический вывод не добавлялись.

## Rollback

- Additive таблицу `worker_kick_state` и schema marker `1.0.17` не удалять.
- При code rollback можно вернуть предыдущий production deployment, сохранив additive schema.
- GitHub workflow остаётся доступным для ручного `workflow_dispatch` и резервного drain.
