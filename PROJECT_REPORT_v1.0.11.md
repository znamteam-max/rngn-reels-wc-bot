# Отчёт по проекту v1.0.11

Статус документа: финальный отчёт по реализации, выпуску и проверке версии.

## Версия и выпуск

- Проект: `rngn-reels-wc-bot`
- Версия: `1.0.11`
- Репозиторий: `znamteam-max/rngn-reels-wc-bot`
- Production URL: `https://project-dcd2y.vercel.app`
- Vercel team: `rngn2`
- Vercel team ID: `team_ILuDVC4YpPUYahSfYxVGtNVv`
- Vercel project: `project-dcd2y`
- Vercel project ID: `prj_qKw2tJ6id8d7foSeavrGRRyZHx8n`
- Исходный commit: `ae27a7baacea4be68fe1763f36938256d1808d0e`
- Проверенный implementation commit: `5391fd802524eb9ef264b3f41a5f3814981e17bf`
- Deployment ID: `7h9EMAf7mnPmLaF6UeMYsxJgJAG8`
- Deployment: `https://vercel.com/rngn2/project-dcd2y/7h9EMAf7mnPmLaF6UeMYsxJgJAG8`

## Preflight и webhook

До выпуска webhook уже указывал на правильный production endpoint:

- URL: `https://project-dcd2y.vercel.app/api/webhook`
- `pending_update_count`: `0`
- `last_error_message`: отсутствует
- `allowed_updates`: `message`, `callback_query`
- Production health до изменений: `ok=true`, версия `1.0.10`

После выпуска webhook установлен повторно на тот же endpoint. Итоговое состояние: `pending_update_count=0`, ошибок нет, список `allowed_updates` не изменился.

## Выполненные изменения

- Добавлена глобальная FIFO-очередь по `videos.status='pending'` с сортировкой `created_at ASC, id ASC`.
- Добавлены singleton-таблица `admin_queue_state`, индекс FIFO и runtime migration.
- `/admin` и `/resend_pending` показывают одну активную карточку вместо пачки заявок.
- Добавлена superadmin-команда `/reset_admin_queue` для восстановления состояния и архивирования старых карточек.
- Новые заявки запускают queue pump и не создают поток полных карточек.
- Действия администраторов блокируют queue state и видео через `FOR UPDATE`, проверяют video ID и message ID.
- Старые callback `adm:*` признаны неактуальными и не могут менять данные. Новые callback используют формат `admq:*`.
- Ручной ввод даты привязан к активным video ID/message ID и защищён claim на 5 минут.
- После обработки карточка превращается в компактный результат без клавиатуры, затем публикуется ровно одна следующая карточка.
- Общий parser дат применяется в admin flow и `/add_znambo`.
- Поддерживаются прошлые, текущие и будущие даты; `DD.MM` всегда трактуется как день и месяц.
- `/api/health` показывает версию, commit SHA и безопасную диагностику очереди.
- В Telegram API добавлены операции изменения клавиатуры и удаления сообщений.
- Исправлены идемпотентная обработка `message is not modified` и сериализация PostgreSQL `date` в JSONB audit log.
- В production-коде отсутствует `WORK_CHAT_ID`.

## Причина ошибки даты

Текста о запрете будущей даты в актуальном репозитории не было, поэтому доказать источник старого пользовательского сообщения по коду нельзя. До выпуска production всё ещё работал на версии `1.0.10`.

В v1.0.11 дата разбирается единым детерминированным parser: `12.07` при текущей дате 16.07.2026 даёт `2026-07-12`, а будущая `20.07` даёт `2026-07-20`. Во время live-проверки дополнительно обнаружена реальная ошибка audit log: объект PostgreSQL `date` передавался в JSONB без преобразования. Теперь дата заранее переводится в ISO-строку.

## Локальные проверки

- Python compile: успешно.
- Python unittest: `49/49` успешно.
- JavaScript tests: `16/16` успешно.
- `12.07` на фиксированной дате 16.07.2026: `2026-07-12`.
- Будущая дата `20.07`: `2026-07-20`.
- Некорректные даты `32.07`, `12.13`, `text`, `2026-02-30`: корректно отклоняются.
- `git diff --check`: успешно.

## Production health

Проверенный ответ `/api/health` для implementation commit:

```json
{
  "ok": true,
  "version": "1.0.11",
  "commit_sha": "5391fd802524eb9ef264b3f41a5f3814981e17bf",
  "missing_env": [],
  "optional_missing_env": [],
  "runtime_migration": {
    "schema": {
      "admin_queue_state": true,
      "idx_videos_pending_fifo": true,
      "admin_queue_main_rows": 1
    }
  },
  "admin_queue": {
    "pending_video_count": 28,
    "active_queue_video_id": 29,
    "active_queue_message_id": 213,
    "active_queue_video_status": "pending"
  }
}
```

## Live-проверки

- До reset в очереди было `29` pending-заявок.
- Успешный `/reset_admin_queue` создал ровно одну активную карточку: video `#6`, message `212`.
- После фактической обработки `#6` очередь автоматически перешла к следующей FIFO-карточке `#29`, message `213`; pending уменьшился с `29` до `28`.
- Ручной ввод `12.07` для активной карточки `#29` выполнен успешно. Активная карточка и количество pending при этом не изменились.
- Callback со старой карточки `adm:a:6:1:0` обработан как stale: active video остался `#29`, pending остался `28`, мутаций данных нет.
- Команды и меню Telegram обновлены успешно.
- Проверен один реальный последовательный переход. Сценарий из трёх последовательных заявок не запускался, чтобы не менять рабочие данные без необходимости.
- Live race двух администраторов не запускался, так как он меняет реальную заявку и Google Sheet. Транзакционная защита покрыта реализацией и локальными тестами.
- Реальный E2E `/add_znambo` не запускался: отсутствовал контролируемый публичный URL с гарантированной очисткой тестовых данных.
- Google Sheets E2E по той же причине не запускался и не заявляется как пройденный.

## Инциденты при выпуске

Первая реализация `/reset_admin_queue` последовательно архивировала 29 старых Telegram-карточек и упиралась в serverless timeout примерно через 26 секунд. Исправление сначала атомарно очищает состояние и запускает новую активную карточку, а затем выполняет ограниченную best-effort архивацию старых сообщений.

Встроенный браузер Codex четыре раза аварийно завершал приложение при открытии Vercel Dashboard. После этого браузерный путь был прекращён. Деплой выполнен через GitHub integration, а production проверен через публичные endpoints и Telegram API.

Локально выгруженное окружение не содержало чувствительный `ADMIN_CHAT_ID`, а старое значение относилось к Telegram-группе до миграции в supergroup. Это повлияло только на синтетическую проверку; production-очередь использовала актуальный чат.

## Итог

Версия `1.0.11` развёрнута в production. Глобальная FIFO-очередь, reset, переход к следующей заявке, stale-card защита и ручной ввод `12.07` подтверждены live-проверками. Не выполненные destructive E2E-сценарии перечислены отдельно и не выданы за успешные.
