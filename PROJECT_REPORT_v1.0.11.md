# Отчёт по проекту v1.0.11

Статус документа: рабочий отчёт, будет дополнен после production-деплоя и live-проверок.

## Версия

- Проект: `rngn-reels-wc-bot`
- Версия: `1.0.11`
- Репозиторий: `znamteam-max/rngn-reels-wc-bot`
- Production URL: `https://project-dcd2y.vercel.app`

## Preflight до изменений

- Telegram webhook: `https://project-dcd2y.vercel.app/api/webhook`
- `pending_update_count`: `0`
- `last_error_message`: отсутствует
- Production health: `ok=true`, версия `1.0.10`
- Vercel project: `project-dcd2y`
- Vercel project ID: `prj_qKw2tJ6id8d7foSeavrGRRyZHx8n`
- Vercel team ID: `team_ILuDVC4YpPUYahSfYxVGtNVv`
- Исходный Git commit: `ae27a7baacea4be68fe1763f36938256d1808d0e`

## Выполненные изменения

- Добавлена единая глобальная FIFO-очередь по `videos.status='pending'` с сортировкой `created_at ASC, id ASC`.
- Добавлена singleton-таблица `admin_queue_state` и runtime migration.
- `/admin` и `/resend_pending` переведены с пачек на одну активную карточку.
- Добавлена superadmin-команда `/reset_admin_queue` для одноразовой архивации старых карточек.
- Новые заявки больше не создают поток полных карточек, а вызывают queue pump.
- Действия администратора блокируют queue state и видео через `FOR UPDATE`, проверяют video/message ID и безопасны при гонке двух админов.
- Старые callback `adm:*` отключены и отвечают alert о неактуальной карточке.
- Новые callback используют формат `admq:*` и всегда содержат video ID.
- Ручной ввод даты привязан к video ID и active message ID; добавлен claim на 5 минут.
- После обработки карточка редактируется в компактный результат без клавиатуры, затем отправляется ровно одна следующая карточка.
- Один детерминированный parser дат используется в admin flow и `/add_znambo`.
- Разрешены прошлые, текущие и будущие даты; `DD.MM` всегда трактуется как день-месяц.
- В `/api/health` добавлены commit SHA и безопасная диагностика FIFO-очереди.
- `WORK_CHAT_ID` не возвращён.

## Локальные проверки

- Python compile: успешно.
- Python unittest: `47/47` успешно.
- JavaScript tests: `16/16` успешно.
- Проверка даты `12.07` на фиксированной дате 16.07.2026: успешно, результат `2026-07-12`.
- Проверка будущей даты `20.07`: успешно, результат `2026-07-20`.
- Проверка invalid дат `32.07`, `12.13`, `text`, `2026-02-30`: успешно.
- `git diff --check`: успешно.

## Production-проверки

- Commit SHA: ожидает фиксации.
- Deployment ID: ожидает деплоя.
- Production alias: ожидает подтверждения после деплоя.
- Health JSON v1.0.11: ожидает деплоя.
- Pending count до reset: ожидает live health.
- Первая активная заявка: ожидает `/reset_admin_queue`.
- Live manual date `12.07`: не заявлена как успешная до фактической проверки.
- Три последовательные заявки: не заявлены как успешные до фактической проверки.
- Stale-card test: локальная логика проверена, live test ожидает деплоя.
- Two-admin race: транзакционная реализация добавлена, live test ожидает деплоя.
- `/add_znambo` real E2E и Google Sheets: не заявлены как успешные до фактической проверки.

## Текущий внешний блокер

Встроенный браузер Codex аварийно завершал приложение при открытии Vercel Dashboard. После четырёх повторов браузерный путь прекращён. Деплой выполнен через GitHub integration и проверяется публичными endpoints и Telegram API.

Первая попытка `/reset_admin_queue` выявила serverless-timeout: последовательная архивация 29 старых Telegram-карточек занимала почти весь лимит функции. Реализация исправлена: queue state очищается и новая активная карточка создаётся до best-effort архивации; число архивных Telegram-запросов за один reset ограничено, а все старые `adm:*` callbacks в любом случае не могут менять данные.

Первый live-повтор ручного `12.07` выявил ещё один idempotency edge case: Telegram отвечает `message is not modified`, если карточка уже содержит ту же дату. Такое редактирование теперь считается успешным, поэтому повторная установка той же даты не откатывает DB-транзакцию.
