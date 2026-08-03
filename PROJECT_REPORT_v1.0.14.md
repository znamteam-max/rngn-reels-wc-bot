# Отчёт по проекту v1.0.14

Статус: production deployment завершён, миграция применена, автоматические тесты пройдены.

## Версия и выпуск

- Проект: `rngn-reels-wc-bot`
- Telegram bot: `@rngn_reels_wc_bot`
- Версия: `1.0.14`
- Репозиторий: `znamteam-max/rngn-reels-wc-bot`
- Ветка: `main`
- Implementation commit: `8fbd7050c547c25045f4d0bf44d2a03e49453ec6`
- Production: `https://project-dcd2y.vercel.app`
- Vercel team/project: `rngn2/project-dcd2y`
- Deployment: `https://vercel.com/rngn2/project-dcd2y/BCvS5FNkKGR2jep3yjGgfAD7HP4m`
- Vercel GitHub status: `success`

## Реализовано

### Обязательная дата в обычных заявках

Для `/new_video` и `/new_bigrecap` после проекта снова запрашивается дата публикации:

```text
платформа -> проект -> дата -> автор -> озвучка -> монтаж -> остальные ссылки -> preview
```

Добавлены состояния `new:date` и `new:date_manual`, callbacks `newdate:today`, `newdate:yesterday`, `newdate:manual` и общий parser даты. При ошибке выводится точное сообщение:

```text
Не понял дату. Используй ДД.ММ или ГГГГ-ММ-ДД.
```

Сессия после ошибки не сбрасывается. Дата хранится в ISO-формате, отображается в preview и обязательна в `normalized_submission_data()` перед insert/update. При повторном редактировании дата сохраняется.

Поток `/add_znambo` не объединялся с новым состоянием и по-прежнему спрашивает дату ровно один раз перед immediate approved.

### Возврат старых заявок без даты

Добавлена admin-only команда:

```text
/return_missing_dates
```

Команда сначала только считает подходящие pending-заявки и показывает подтверждение `Да, вернуть` / `Отмена`. Изменения выполняются только callback `missingdate:return`.

После подтверждения бот:

- переводит только `pending` с `publish_date IS NULL` в `needs_revision`;
- сохраняет исходную строку, ссылки, проект, роли, тип и submitter;
- записывает проверившего админа и audit logs `missing_date_returned`, `missing_dates_bulk_returned`;
- пересчитывает затронутые batches;
- очищает FIFO pointer, если активная заявка попала в возврат;
- архивирует старую active card без клавиатуры;
- обновляет dashboard и запускает следующий FIFO item;
- уведомляет submitter с кнопкой `Указать дату` (`revdate:<video_id>`);
- считает ошибки Telegram-доставки, не возвращая статус заявки обратно.

### Короткое исправление даты

Для `needs_revision` без даты `/my_requests` теперь показывает `Указать дату`, а не полную повторную форму.

Доступ проверяется по `added_by_tg_id`; также разрешён admin. Поддерживаются callbacks:

```text
revdate:set:<id>:today
revdate:set:<id>:yesterday
revdate:manual:<id>
```

После корректной даты меняются только дата, статус и audit-поля проверки: заявка возвращается в `pending`, batch пересчитывается, dashboard обновляется, FIFO перепроверяется. Все содержательные поля заявки сохраняются.

### Егор Петрушков как монтажёр

Runtime migration создаёт или обновляет role-specific запись:

```text
Егор Петрушков (@RayBallPro)
role=montage
is_active=true
```

Seed идемпотентен и деактивирует только дубли, совпадающие по монтажной роли и точному имени/username. Авторская запись `Егор` не переименовывается.

Backfill изменяет только видео с точным `montage_name = 'Егор Петрушков'` и пустым `montage_username`: устанавливает `RayBallPro` и безопасно разрешённый `montage_id`. Строки с другим именем или непустым другим username не меняются.

### Команды и health

В `scripts/setup_bot_ui.py` добавлена команда:

```text
return_missing_dates — Вернуть заявки без даты
```

Help сообщает, что дата публикации обязательна. В `/api/health` добавлены безопасные блоки без Telegram IDs:

```json
{
  "missing_publish_date": {
    "pending": 61,
    "needs_revision": 1
  },
  "egor_montage": {
    "active_rows": 1,
    "backfilled_videos": 2
  }
}
```

## Production migration

Первый health после deployment подтвердил:

- runtime migration: `applied=true`;
- Egor seed action: `inserted`;
- активных монтажных строк Егора: `1`;
- точечно backfilled видео в этом запуске: `2`;
- текущих видео с полным snapshot Егора: `2`;
- active projects: `9`;
- `missing_env=[]`;
- `optional_missing_env=[]`.

## Очередь до и после deployment

До deployment, на commit `0e70805894cc8c5c18d1caed0cc16eb79cff9073`:

```json
{
  "pending_video_count": 62,
  "active_queue_video_id": 42,
  "active_queue_message_id": 256,
  "dashboard_message_id": 234,
  "queue_filter_type": "global"
}
```

После deployment, на commit `8fbd7050c547c25045f4d0bf44d2a03e49453ec6`:

```json
{
  "pending_video_count": 62,
  "active_queue_video_id": 42,
  "active_queue_message_id": 256,
  "dashboard_message_id": 234,
  "queue_filter_type": "global",
  "missing_publish_date": {
    "pending": 61,
    "needs_revision": 1
  }
}
```

Deployment и runtime migration не меняли статусы очереди. `/return_missing_dates` не запускалась, потому что в этой сессии не было явного Telegram-подтверждения массового возврата.

Поэтому live-результаты bulk action честно остаются не сформированными:

- переведено в `needs_revision`: не запускалось;
- успешно уведомлено: не запускалось;
- ошибки уведомления: не запускалось;
- live author revision: не запускался;
- pending до/после bulk: `62 / не запускалось`.

## Webhook и Telegram

- Production route `GET /api/webhook` отвечает `200 OK`, `ok=true`, service `rngn-reels-wc-bot`.
- Production health подтверждает отсутствие пропущенных обязательных env.
- Повторный Telegram `getWebhookInfo` и применение обновлённого списка commands не выполнялись: корректный production token RNGN недоступен локально.
- Локальный `.env.local` относится к другому Telegram-боту и намеренно не использовался.
- Controlled live submission/date revision не создавались без корректной защищённой Telegram-сессии и безопасной тестовой ссылки.

## Тесты

- Python unittest: `96/96` успешно.
- JavaScript tests: `16/16` успешно.
- Python `compileall`: успешно.
- Production entrypoint imports: успешно.
- `npm run check`: успешно.
- `git diff --check`: успешно.
- Runtime scan: `WORK_CHAT_ID` отсутствует в `bot`, `api`, `scripts`.

Новые тесты покрывают date picker, ручную и ошибочную дату, обязательность даты, big recap regression, сохранение `/add_znambo`, подтверждение bulk action, возврат пяти строк, ошибки уведомления, восстановление FIFO, доступ владельца и запрет чужому пользователю, короткую дату в `/my_requests`, идемпотентный seed Егора, точный backfill, picker и health diagnostics.

## Изменённые файлы

- `bot/handlers.py`
- `bot/people_seeds.py`
- `bot/runtime_migrations.py`
- `api/health.py`
- `scripts/init_db.py`
- `scripts/setup_bot_ui.py`
- `tests/test_links.py`
- `tests/test_projects_dashboard.py`
- `tests/test_v1014_author_dates_egor.py`
- `PROJECT_REPORT_v1.0.14.md`

## Итог

Версия `1.0.14` развёрнута в production. Новые авторские заявки требуют дату до выбора автора, старые заявки без даты можно безопасно вернуть только после admin confirmation, а владелец может исправить одну дату без повторного заполнения формы. Егор доступен в montage picker как `Егор Петрушков (@RayBallPro)`, production seed дал одну активную строку и backfill двух точных видео. Массовая операция и Telegram live E2E не выдаются за выполненные без фактического подтверждения и production credentials.
