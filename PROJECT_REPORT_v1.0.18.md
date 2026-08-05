# PROJECT REPORT v1.0.18

Дата: 2026-08-05  
Проект: `@rngn_reels_wc_bot`  
Repository: `znamteam-max/rngn-reels-wc-bot`  
Production: `https://project-dcd2y.vercel.app`

## Итоговый статус

Версия и schema marker `1.0.18` развёрнуты в production. Активная FIFO-карточка теперь резервируется и фиксируется в PostgreSQL до вызова Telegram, а dashboard больше не выбирает, не отправляет и не ремонтирует очередь. Продвижение очереди выполняется синхронно после успешного admin action и независимо от dashboard, Google Sheets, фонового worker и ручных `/admin`, `/resend_pending`, `/reset_admin_queue`.

Решающая проверка выполнена на production-инфраструктуре в изолированных PostgreSQL TEMP TABLE: 10 последовательных действий, 10/10 автоматических переходов, 0 дубликатов, 0 stale callbacks, 0 ручных repair-команд. Пользовательские video statuses и рабочий active pointer не изменялись.

## Коммиты и deployment

- Основной implementation commit: `0ed09cc` (`fix: make admin FIFO reservation atomic`).
- Исправление nullable PostgreSQL parameters: `9c3612d`.
- Изоляция production acceptance и orphan watchdog: `2484742`.
- Точечная очистка тестовой карточки: `107f674`.
- Финальный runtime cleanup commit: `c06a9445237bfd78ecd7a16b3ccddf34871cadd9` (`ops: remove v1.0.18 rollout probe`).
- Финальный проверенный runtime deployment ID: `5dCZDUZrmVc4UkNf3RoixcQiwQDA`, status `success`.
- Production URL: `https://project-dcd2y.vercel.app`.
- Health после cleanup: HTTP 200, `ok=true`, `version=1.0.18`, `schema_version=1.0.18`, commit `c06a944...`.

Промежуточные deployment IDs, необходимые для миграции и E2E: `tepukLsyupPwEY9YHo7xrJCavUjX`, `BcsWAJocTkWFHiMKzJMkJpKga4XS`, `be2whXz4zQrRkdKBgnJDhjk9x8qA`, `2GzFUYUTFDaJsU9bKuohTfdQT8He`, `Fe1asbaZkLe3xAbNCCfYuAmrYqhD`, `3aRGa5PxwK4fr2CynSkQdqaqxonN`.

## Реализация atomic FIFO

Создан единый модуль `bot/admin_queue.py`. Он является владельцем live FIFO state и предоставляет:

```text
reserve_next_pending_card(...)
deliver_reserved_card(...)
pump_queue_live(...)
complete_active_action(...)
repair_queue_if_needed(...)
get_queue_diagnostics(...)
```

Основные свойства реализации:

- singleton `admin_queue_state` блокируется через `FOR UPDATE`;
- следующий pending выбирается строго по `created_at, id` с `FOR UPDATE SKIP LOCKED` и действующим queue filter;
- исторический `videos.admin_message_id` не исключает запись из FIFO;
- UUID reservation, timestamp, generation и delivery attempts сохраняются с commit до Telegram send;
- после send message ID записывается только при совпадении video, token и generation;
- send failure освобождает только свою reservation и оставляет video pending;
- send success + pointer-save failure создаёт высокоприоритетный adoption job вместо повторной отправки;
- watchdog повторяет stale reservation того же video после 5 секунд и очищает orphan delivery fields;
- non-active pending message metadata очищается в БД без массового редактирования Telegram-сообщений;
- команды `/queue_debug` и superadmin-only `/queue_trace <video_id>` показывают безопасную диагностику без секретов и полных callback payloads.

Admin completion выполняется в порядке:

```text
business mutation + active pointer clear COMMIT
-> answer callback
-> finalize old Telegram card
-> synchronous next queue pump
-> async dashboard / Sheets / result notification
```

Ошибка финального edit старой карточки или dashboard не восстанавливает старый pointer и не останавливает следующую карточку.

## Dashboard isolation

Dashboard разделён на snapshot/render/refresh path. `refresh_admin_dashboard` обновляет только:

```text
dashboard_chat_id
dashboard_message_id
dashboard_updated_at
```

Он не выбирает pending video, не отправляет review card, не вызывает queue pump и не меняет `active_*` или `videos.admin_message_id`.

Доказательство:

- глобальный поиск live SQL оставил операции active FIFO в `bot/admin_queue.py`; исключения только migration/test fixtures;
- regression guard `test_dashboard_and_other_modules_cannot_write_active_fields` анализирует SQL/исходники и запрещает запись `active_*` из dashboard и посторонних модулей;
- `test_old_card_and_dashboard_failures_do_not_stop_next_pump` подтверждает продолжение FIFO при контролируемой ошибке dashboard;
- worker handler `dashboard_refresh` вызывает только read-only refresh path и является terminal best-effort job без retry, блокирующего queue pump.

## Worker и persistent failures

Приоритеты jobs:

```text
admin_queue_pump = 5
dashboard_refresh = 20
telegram result notification = 40
Google Sheets = 60+
```

В `background_jobs` добавлены `first_error`, `first_failed_at`, `last_failed_at`, `failure_count`. На первой ошибке причина и время сохраняются через `COALESCE`, при следующих ошибках обновляются last fields и счётчик. После успешного retry job становится `done`, `last_error` может быть очищен, но first-error diagnostics сохраняются.

Regression test `test_first_job_error_survives_success_sql` проверяет оба SQL path: failure записывает immutable first error и увеличивает count, success не обнуляет эти поля. Финальный production health: `done_after_retry=0`; это текущее значение, а не потеря diagnostics.

## Schema migration и repair

Миграция additive и идемпотентная.

Добавлено в `admin_queue_state` 8 колонок:

```text
active_reservation_token
active_reserved_at
active_generation
active_delivery_attempts
active_last_error
active_last_error_at
last_repaired_at
last_repair_reason
```

Добавлено в `background_jobs` 4 колонки:

```text
first_error
first_failed_at
last_failed_at
failure_count
```

Первый migration attempt обнаружил реальную PostgreSQL-ошибку `IndeterminateDatatype` для nullable параметров и полностью откатился. Параметры были исправлены явными casts `::bigint` / `::text`; повторный запуск успешно применил все 12 колонок и schema marker.

Успешная migration snapshot, `2026-08-05T12:51:53Z`:

```text
BEFORE
active_video_id=null
active_chat_id=null
active_message_id=null
active_status=null
pending=0
pending_with_admin_message_id=0
non_active_stale_pending_message_ids=0

AFTER
active_video_id=null
active_chat_id=null
active_message_id=null
active_reservation_token_present=false
active_generation=0
active_delivery_attempts=0
pending=0
non_active_stale_pending_message_ids=0
```

Migration action: `invalid_pointer_cleared`; stale non-active pending message IDs cleared: `0`. Ни один video status миграцией не изменён. Следующие вызовы вернули `already_applied`.

## Concurrency и regression tests

Проверены сценарии:

- две параллельные admin callbacks: принимается только первая, вторая получает stale-card result;
- два queue pumps: создаётся одна reservation и отправляется одна карточка;
- reservation commit до Telegram send;
- recent reservation не дублируется, stale reservation повторяет тот же video;
- invalid active status очищается и резервируется FIFO head;
- send failure освобождает совпадающую reservation;
- pointer-save failure ставит adoption job для уже отправленного message;
- отсутствующий active pointer автоматически pump-ится;
- orphan message/chat fields очищаются при пустом active pointer;
- old-card edit failure и dashboard failure не блокируют next pump;
- queue filters, project filters и unassigned semantics версии 1.0.17 сохранены;
- migration очищает только metadata non-active pending rows;
- nullable PostgreSQL parameters имеют явные типы;
- все event-kick regression tests версии 1.0.17 проходят.

Локальная проверка:

```text
Python: 160 passed, 21 subtests passed
Node: 24/24 passed
python compileall: passed
git diff --check: passed
```

`ruff` в локальном окружении не установлен; syntax, full Python/Node suites и diff checks пройдены.

## Production-isolated 10-action acceptance

Безопасные genuine decisions для 10 пользовательских production-записей не были предоставлены, поэтому статусы реальных видео не фабриковались. В соответствии с заданием тест выполнен в production database connection через PostgreSQL TEMP TABLE shadows для `videos`, `admin_queue_state` и `logs`.

Это использовало production runtime, реальный PostgreSQL engine и настоящие commit boundaries, но fake Telegram transport и изолированные fixture rows. TEMP TABLE были явно удалены, connection очищен до возврата в pool. До и после acceptance рабочая очередь была идентична:

```text
active_video_id=195
active_message_id=418
active_status=pending
FIFO head=195
pending=5
non_active_stale_pending_message_ids=0
```

Результат decisive run:

```text
isolated=true
isolation=postgres_temp_tables
committed_transactions=true
cleaned_up=true
actions=10
advanced=10
sent_cards=11
manual_repair_commands=0
duplicate_cards=0
stale_callbacks=0
final_fixture_active_equals_fifo_head=true
final_fixture_status=pending
final_fixture_message_id=900011
```

В mix вошли `approved`, `duplicate`, `needs_revision`. Не использовались `/admin`, `/resend_pending`, `/reset_admin_queue`. После каждого действия active video был pending, соответствовал oldest eligible row, message pointer сохранялся автоматически, а non-active stale metadata оставалась нулевой.

### Per-stage latency

Все значения в миллисекундах:

| Stage | Samples | Average | Max | Raw |
|---|---:|---:|---:|---|
| Action total | 10 | 60.4 | 80 | 80, 56, 54, 75, 67, 59, 51, 56, 53, 53 |
| Mutation commit | 10 | 20.3 | 36 | 28, 17, 15, 36, 16, 23, 15, 19, 18, 16 |
| Next reservation | 10 | 10.3 | 18 | 18, 10, 10, 9, 12, 9, 9, 10, 8, 8 |
| Card send | 10 | 1.1 | 2 | 1, 1, 1, 1, 1, 1, 1, 2, 1, 1 |
| Pointer saved | 10 | 8.7 | 12 | 10, 8, 7, 10, 12, 7, 8, 7, 9, 9 |

Максимальная фиксация next-card pointer заняла 12 ms, что значительно ниже критерия 5 секунд.

## Инцидент во время acceptance и исправление

Первая версия production acceptance ошибочно использовала committed fixture rows в публичной `videos`. Global queue смог увидеть одну такую запись, поэтому этот прогон не засчитан.

Фактическое воздействие ограничено одной тестовой fixture-карточкой:

```text
fixture video #182
Telegram message #417
adoption job #157
```

Fixture rows и связанные statuses удалены; пользовательские rows/statuses не изменялись. Сообщение #417 точечно отредактировано в архивную тестовую карточку с отключёнными кнопками. Массовое редактирование Telegram сообщений не выполнялось. После инцидента acceptance полностью переведён на TEMP TABLE isolation, а watchdog дополнен очисткой orphan delivery fields. Именно изолированный повторный прогон выше является decisive acceptance.

## Финальный production snapshot

Snapshot `2026-08-05T13:11:21.908682Z`:

```text
ok=true
version=1.0.18
schema_version=1.0.18
commit_sha=c06a9445237bfd78ecd7a16b3ccddf34871cadd9

jobs enabled=true
jobs queued=0 ready=0 future=0 processing=0 failed=0 dead=0
jobs stale_processing=0 done_after_retry=0
worker healthy=true state=idle source=event_kick last_error=null
worker_kick lease_active=false accepted_count=62 last_error=null
sheets queued_videos=0 failed_videos=0
telegram failed_last_hour=0 processing_stale=0
bulk_operations active=0
webhook samples=166 p50=587.0ms p95=1377.2ms target=3000ms

queue pending=5
active_video_id=195
active_chat_id=-1004364338370
active_message_id=418
active_status=pending
active_reservation_present=true
active_generation=2
active_delivery_attempts=2
active_last_error=null
queue_filter=global
non_active_stale_pending_message_ids=0
FIFO head=195
dashboard_message_id=234
dashboard_updated_at=2026-08-05T13:02:09.026277Z
```

Oldest pending FIFO snapshot после acceptance:

```text
#195  created_at=2026-08-05T12:55:04.716340Z  ACTIVE
#197  created_at=2026-08-05T12:56:21.408725Z
#199  created_at=2026-08-05T12:57:59.068774Z
#202  created_at=2026-08-05T12:59:25.246641Z
#208  created_at=2026-08-05T13:02:08.609007Z
```

Active pointer соответствует pending FIFO head, reservation сохранена, active message присутствует, stale metadata равна нулю. Dashboard и Sheets работают асинхронно, event-kick продолжает дренировать jobs.

## Safety и ограничения

- Реальные 10 admin decisions не выполнялись: нельзя достоверно выбрать approved/duplicate/revision за администраторов. Использована прямо разрешённая production-isolated fixture.
- Mass `/return_missing_dates` не запускался.
- `/admin`, `/resend_pending`, `/reset_admin_queue` в decisive acceptance не использовались.
- Bulk return не запускался.
- Production secrets, reservation UUID, полные callback payloads и значения env не выводились и не добавлялись в git.
- `WORK_CHAT_ID` отсутствует в runtime-коде/config (`bot`, `api`, `scripts`) и удалён из Production Environment Variables проекта `project-dcd2y` в Vercel. После удаления отсутствие подтверждено на странице настроек; значение переменной не раскрывалось.

## Rollback

- Additive queue/job columns и schema marker не удалять.
- При необходимости откатить только application code на предыдущий production deployment.
- Не восстанавливать старые non-active `admin_message_id` metadata.
- Не менять уже принятые video statuses.
- Не откатывать рабочую очередь на тестовые fixture state/message IDs.

## Решение по критерию приёмки

Технический defect atomic FIFO закрыт не только unit-тестами: decisive production-isolated run прошёл 10 последовательных действий без repair-команд, и каждая следующая карточка стала активной автоматически. Рабочий production pointer после теста остался неизменным и согласованным.
