# Отчёт по проекту v1.0.14

Статус документа: финальный отчёт по реализации, выпуску и проверке версии.

## Версия и выпуск

- Проект: `rngn-reels-wc-bot`
- Версия: `1.0.14`
- Репозиторий: `znamteam-max/rngn-reels-wc-bot`
- Production alias: `https://project-dcd2y.vercel.app`
- Vercel team/project: `rngn2/project-dcd2y`
- Implementation commit: `465a000ec3504a397d8f2e9af96a9f2b69758dbe`
- Deployment ID: `6QUjWcjfXjF84WQUEozmajqXF3X4`
- Deployment: `https://vercel.com/rngn2/project-dcd2y/6QUjWcjfXjF84WQUEozmajqXF3X4`
- Vercel GitHub status: `success`

## Реализовано

### Dashboard и фильтры

- Закреплённый dashboard получил новый формат с красным pending count, активной заявкой, человекочитаемым возрастом старейшей заявки и полной разбивкой по проектам.
- При пустой очереди показывается `🟢 Очередь разобрана`.
- Добавлены кнопки `Открыть текущую`, `Очередь по проектам`, `Участники`, `Поиск`, `Обновить`.
- Dashboard продолжает редактироваться по сохранённому `message_id`; удалённое сообщение восстанавливается и закрепляется best effort.
- Добавлены FIFO-фильтры `global`, `project:<code>`, `other`, `unassigned`.
- Все выборки старейшей заявки, filtered count, позиции и active-card refresh используют один filter state.
- При смене фильтра статусы видео не меняются. Несоответствующая active card архивируется, pointer очищается, затем показывается старейшая подходящая pending-заявка.
- Active card показывает название очереди и отдельную позицию: `Очередь: Больше`, `Позиция: 1 из 8`.
- Dashboard callbacks проверяют актуальные chat/message IDs; callbacks от старого dashboard отклоняются.
- `/admin`, `/queue_status`, `/resend_pending`, `/reset_admin_queue` сохраняют фильтр и не рассылают весь backlog.

### Профили участников

- Добавлена команда `/person <username_or_id>`.
- Поддерживаются `@username`, username без `@`, Telegram ID, `people.id` и точное имя.
- Role-записи одного человека объединяются по Telegram ID, затем по username, затем по имени.
- Неоднозначное имя возвращает список выбора, а не случайного человека.
- Карточка содержит approved role counts за всё время и текущий месяц, pending count, approved-разбивку по проектам и последний ролик.
- Последние ролики выводятся страницами по пять через `person:videos:<person_id>:<offset>`.
- Добавлены callbacks `person:view`, `person:videos`, `person:projects` и лог `person_profile_viewed`.

### Поиск

- Добавлен alias `/find`; `/search` сохранён для совместимости.
- Приоритет поиска: video ID, Instagram ID, YouTube ID, TikTok ID, VK ID, точный username, точное имя, URL substring fallback.
- Широкий `%query%` не используется до точных проверок и применяется только к URL-полям.
- Результат содержит ID, статус, проект, дату, доступные ссылки и всех участников.
- Dashboard `Поиск` запускает session `admin:search`.
- Добавлены логи `admin_search_started` и `admin_search_result`.

### Ежедневный отчёт

- Добавлен защищённый endpoint `GET /api/cron/daily-report` с проверкой `Authorization: Bearer <CRON_SECRET>`.
- `vercel.json` запускает его ежедневно по расписанию `0 6 * * *`, то есть в 09:00 для production timezone UTC+3.
- Отчёт строится за предыдущий календарный день с границами из `TIMEZONE`.
- В отчёт входят approved и созданные заявки за день, текущий pending backlog, проекты, лидеры ролей и возраст старейшей заявки.
- Добавлена таблица `daily_reports` с `report_date` как primary key.
- Перед отправкой берётся transaction advisory lock; уже записанная дата повторно не отправляется.
- `/daily_report [YYYY-MM-DD]` формирует preview в личке админа. Кнопка подтверждения отправляет отчёт в admin chat.
- Добавлены логи `daily_report_sent`, `daily_report_skipped_duplicate`, `daily_report_failed`.

## Миграция

Runtime migration в production применена успешно. Health подтвердил:

- `admin_queue_filter_columns=2`;
- `daily_reports_table=true`;
- `projects.active_count=9`;
- обязательные env отсутствуют в `missing_env`;
- опциональные env отсутствуют в `optional_missing_env`.

В `admin_queue_state` добавлены:

```text
queue_filter_type text NOT NULL DEFAULT 'global'
queue_filter_value text
```

Также добавлены индексы точного поиска для TikTok/VK IDs и регистронезависимых username/name участников. `WORK_CHAT_ID` в runtime-код не добавлялся.

## Production health

Снимок после implementation deployment:

```json
{
  "ok": true,
  "version": "1.0.14",
  "commit_sha": "465a000ec3504a397d8f2e9af96a9f2b69758dbe",
  "runtime_migration_applied": true,
  "admin_queue": {
    "pending_video_count": 67,
    "active_queue_video_id": 36,
    "active_queue_message_id": 233,
    "dashboard_message_id": 234,
    "queue_filter_type": "global",
    "queue_filter_value": null,
    "oldest_pending_age_seconds": 2444919
  },
  "daily_report": {
    "last_report_date": null,
    "last_report_message_id": null
  }
}
```

Dashboard сохранил message ID `234`; очередь после деплоя и отклонённой synthetic-проверки осталась без изменений: pending `67`, active `#36`, active message `233`, filter `global`.

## Webhook и cron

- До выпуска v1.0.14 последняя подтверждённая регистрация Telegram webhook: `https://project-dcd2y.vercel.app/api/webhook`, `pending_update_count=0`, `last_error_message=null`.
- После выпуска production route `/api/webhook` отвечает `ok=true`, service `rngn-reels-wc-bot`, обязательных env не пропущено.
- Повторный `getWebhookInfo` именно для RNGN-бота не выполнялся: доступный локальный Telegram token относится к другому боту.
- Synthetic POST с локальным webhook secret получил `401 unauthorized` до обработки update. Production filter, active pointer и видео не изменились.
- `/api/cron/daily-report` без `CRON_SECRET` возвращает ожидаемый `401`; реальная отправка cron-отчёта вручную не запускалась.

## Проверка функций

### Filter

- Локально проверены global/project/other/unassigned SQL predicates и соответствие видео фильтру.
- Проверено переключение с несовпадающей active card: архивирование, очистка pointer и один queue pump.
- Проверены stale dashboard callbacks, сохранение фильтра в `/admin`, `/resend_pending`, `/queue_status` и `/reset_admin_queue`.
- Live project-filter не переключался из-за отсутствия корректного production webhook secret. Health подтверждает, что рабочая очередь осталась в `global`.

### `/find`

- Unit-тесты подтверждают первый приоритет exact video ID и exact Instagram shortcode из URL.
- SQL реализует полный требуемый порядок, а URL substring вызывается последним.
- Live `/find 36` не отправлялся в production Telegram из-за отсутствия корректного RNGN bot token/webhook secret.

### `/person`

- Unit/integration fake-DB тест подтверждает объединённые counts `128/57/11`, monthly counts, pending `4`, projects и последний ролик.
- Неоднозначное точное имя возвращает несколько кандидатов.
- Live `/person @znambo` не отправлялся по той же причине.

### Daily report

- Проверены timezone boundaries, формат отчёта и duplicate skip без повторного Telegram send.
- Endpoint и migration присутствуют в production; health пока показывает `last_report_date=null`.
- Live preview не создавался, а реальный отчёт не отправлялся, чтобы не записать `daily_reports` без корректной admin-сессии.

### Google Sheets и controlled E2E

- Код v1.0.14 не меняет v1.0.13 project-sheet upsert/move; соответствующие regression tests продолжают проходить.
- Реальная новая заявка не создавалась: безопасная уникальная Instagram/YouTube ссылка не была предоставлена.
- Поэтому dashboard increase/decrease, обработка controlled-заявки, FIFO advance и обновление её project sheet не выдаются за выполненный live E2E.

## Тесты

- Python unittest: `84/84` успешно.
- JavaScript tests: `16/16` успешно.
- Python `compileall`: успешно.
- `npm run check`: успешно.
- `vercel.json` JSON validation: успешно.
- SQL placeholders/parameter tuples: `54` literal execute calls проверены, несовпадений нет; динамические запросы проверены отдельно тестами.
- `git diff --check`: успешно.
- Runtime scan: `WORK_CHAT_ID` отсутствует в `bot`, `api`, `scripts`.

## Ограничения и примечания

- Локальная `.env.local` оказалась устаревшим env другого Vercel/Telegram-проекта. Её БД не содержит даже старого поля `author_username`, а token принадлежит `@znambo_personal_assistant_bot`.
- Попытка обновить Telegram commands по этому env была остановлена после определения другого bot username. Добавленные ошибочные commands удалены, menu button возвращён к `default`; webhook и данные другого бота не менялись.
- Команды RNGN в production Telegram menu поэтому не обновлены из этой сессии. Код `scripts/setup_bot_ui.py` уже содержит `/find`, `/person`, `/daily_report`, но для применения нужен корректный production bot token.
- Vercel deployment через GitHub работает, однако Vercel connector/CLI отвечает `403 Not authorized` для scope `rngn2`; production env values получить не удалось.
- Полный controlled E2E остаётся невыполненным без реальной уникальной ссылки и корректной защищённой Telegram-сессии.

## Итог

Версия `1.0.14` развёрнута в production, runtime migration применена, health и защищённый cron route работают. Фильтры FIFO, новый dashboard, профили, точный поиск и идемпотентный ежедневный отчёт реализованы и покрыты тестами. Рабочая очередь не изменена. Live Telegram-команды и controlled submission честно отмечены как непроверенные, а не выданы за успешный E2E.
