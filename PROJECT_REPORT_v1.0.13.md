# Отчёт по проекту v1.0.13

Статус документа: финальный отчёт по реализации, выпуску и проверке версии.

## Версия и выпуск

- Проект: `rngn-reels-wc-bot`
- Версия: `1.0.13`
- Репозиторий: `znamteam-max/rngn-reels-wc-bot`
- Production alias: `https://project-dcd2y.vercel.app`
- Vercel team: `rngn2`
- Vercel project: `project-dcd2y`
- Implementation commit: `d3937c7ce971576585ba6e3046247480b6c0043f`
- Deployment ID: `FP3ysazEctPYdSjkfVBLjLJ7dePt`
- Deployment: `https://vercel.com/rngn2/project-dcd2y/FP3ysazEctPYdSjkfVBLjLJ7dePt`
- Vercel status: `success`, `Deployment has completed`

## Реализовано

- Во всех трёх потоках `/new_video`, `/new_bigrecap` и `/add_znambo` после уникальной ссылки добавлен обязательный выбор проекта.
- Сохранён YouTube-first порядок `/new_bigrecap`; после проекта обычный и big recap потоки продолжаются с выбора автора, а `/add_znambo` — с выбора даты.
- Для `Другой проект` добавлен ручной ввод названия длиной 2–60 символов с запретом ссылок. Постоянная запись проекта при этом не создаётся.
- В ревизиях проект сохраняется. Старые ревизии без проекта сначала направляются на выбор проекта.
- Проект показывается в preview, финальных сообщениях и карточке активной admin-очереди. Для старых строк выводится `не указан`.
- Старую заявку без проекта нельзя одобрить, пока администратор не назначит проект через `Сменить проект`.
- Добавлен постоянный admin-дашборд: общий pending, активная заявка, возраст самой старой заявки и разбивка по проектам.
- Дашборд редактируется на месте, восстанавливается после удаления и закрепляется best effort. Добавлены `/queue_status` и callbacks `dash:open`, `dash:refresh`, `dash:projects`.
- Глобальный FIFO сохранён: одновременно показывается ровно одна активная карточка; `/admin`, `/resend_pending` и `/reset_admin_queue` не рассылают весь backlog.
- В Google Sheets добавлены поля проекта, девять проектных листов, `Project Stats` и `People × Projects`; upsert и перенос между проектными листами идемпотентны.
- Health endpoint дополнен безопасной диагностикой проектов и постоянного дашборда.
- `WORK_CHAT_ID` в код не возвращён.

## Миграция и seed

Runtime migration применена успешно. В production подтверждены:

- таблица `projects`;
- три project-поля в `videos`;
- индексы `idx_videos_project_id` и `idx_videos_status_project`;
- три dashboard-поля в `admin_queue_state`;
- singleton `admin_queue_state` для `queue_name='main'`.

Первая сборка implementation commit `9c8f733` обнаружила проблему порядка миграции существующей БД: project-индекс создавался до `ALTER TABLE`. Порядок исправлен в `d3937c7`, добавлен regression test, повторный production deployment завершён успешно.

Идемпотентно созданы ровно девять активных проектов:

| code | name | sort_order |
|---|---|---:|
| `vzyal_myach` | Взял Мяч | 10 |
| `bolshe` | Больше | 20 |
| `ves_sport` | Весь Спорт | 30 |
| `padel_channel` | Padel Channel | 40 |
| `home_of_hockey` | Home of Hockey | 50 |
| `double_play` | Double Play | 60 |
| `sport_core` | Sport Core | 70 |
| `music_core` | Music Core | 80 |
| `other` | Другой проект | 999 |

`Коробка` и `Fish and Chips` не добавлялись. В health подтверждено `active_count=9`; старые видео не классифицировались автоматически, `videos_without_project=123`.

## Production-проверка

Снимок production после исправленного деплоя:

```json
{
  "ok": true,
  "version": "1.0.13",
  "commit_sha": "d3937c7ce971576585ba6e3046247480b6c0043f",
  "runtime_migration_applied": true,
  "active_projects": 9,
  "videos_without_project": 123,
  "pending_video_count": 67,
  "active_queue_video_id": 36,
  "active_queue_message_id": 233,
  "dashboard_message_id": 234,
  "dashboard_updated_at": "2026-08-03T14:17:44.319032+00:00"
}
```

### Webhook до и после

- До выпуска v1.0.13: версия `1.0.12`, URL `https://project-dcd2y.vercel.app/api/webhook`, `pending_update_count=0`.
- После выпуска v1.0.13: тот же правильный URL, `ok=true`, `pending_update_count=0`, `last_error_message=null`, allowed updates: `message`, `callback_query`.

### Дашборд и FIFO

- Dashboard message ID: `234`.
- Telegram подтвердил, что сообщение `234` закреплено в admin supergroup.
- Повторный `dash:refresh` сохранил message ID `234` и изменил только `dashboard_updated_at`, то есть дашборд редактируется на месте.
- Pending до создания/обновления дашборда: `67`; после: `67`. Создание дашборда не изменило рабочие данные.
- Active video ID до и после: `36`; active card message ID: `233`.
- Защищённая production-проверка попытки одобрить старую активную заявку без проекта не изменила очередь: pending осталось `67`, активной осталась заявка `36`. Блокировка `Сначала укажи проект.` сработала без записи в БД.
- Полная обработка заявки не запускалась, поэтому рабочая FIFO-очередь не менялась и новые полные карточки не рассылались.
- Telegram commands/menu обновлены; `/queue_status` опубликована.

## Google Sheets

- Локальными unit/integration тестами подтверждены project-поля в `Videos`, идемпотентный upsert в проектный лист и перенос видео между проектными листами без дублей.
- Формирование `Project Stats` и `People × Projects` реализовано; role counts берутся только из approved-видео.
- Реальная production-синхронизация проектного листа в этой проверке не запускалась, поскольку контролируемая заявка не создавалась.

## Тесты

- Python unittest: `69/69` успешно.
- JavaScript tests: `16/16` успешно.
- `npm run check`: успешно.
- Python `compileall`: успешно.
- Проверка SQL placeholders/parameter tuples в обработчиках: успешно.
- `git diff --check`: успешно.
- Regression test порядка runtime migration на существующей БД: успешно.

Покрыты обязательный выбор проекта во всех потоках, custom project, сохранение проекта в ревизии, блокировка старой заявки без проекта, одна активная FIFO-карточка, edit-in-place и восстановление дашборда, обновление счётчика при новой pending-заявке, переход к следующей заявке, идемпотентный project-sheet upsert и перенос между листами.

## Ограничения live E2E

- Контролируемая реальная заявка в production не создавалась: безопасная уникальная Instagram/YouTube-ссылка не была предоставлена, а вымышленная запись загрязнила бы рабочую БД и Google Sheets.
- Поэтому полный live-проход через project picker, уменьшение dashboard count после обработки, переход FIFO к следующей заявке и обновление конкретного project sheet честно отмечены как не выполненные в production.
- `/add_znambo` с реальной ссылкой также не запускался; его project → date → approved → Sheets сценарий подтверждён локальными тестами.
- Локальный transaction smoke against production DB не запускался: скачанные Vercel env-файлы содержали пустое значение `DATABASE_URL`. Применение миграции подтверждено самим production runtime и `/api/health`.

## Итог

Версия `1.0.13` развёрнута в production. Обязательная маршрутизация по проектам, назначение проекта администратором, project-aware карточки и Google Sheets, а также единый закреплённый дашборд FIFO-очереди реализованы. Production health, runtime migration, seed из девяти проектов, webhook, pin и редактирование дашборда на месте подтверждены; проверки, требующие создания реальной рабочей заявки, не выдаются за выполненный live E2E.
