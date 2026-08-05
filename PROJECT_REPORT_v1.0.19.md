# PROJECT REPORT v1.0.19

Дата: 2026-08-05  
Проект: `@rngn_reels_wc_bot`  
Production: `https://project-dcd2y.vercel.app`  
Статус: **read-only аудит завершён, изменения данных и rebuild ожидают подтверждения**

## Версия и доставка

- Версия приложения и схемы: `1.0.19`.
- Основной implementation commit: `d969343` (`feat: add v1.0.19 sheets reconciliation`).
- Production проверен на v1.0.19; текущий audit commit: `f6844d7`.
- Additive migration применена успешно.
- В БД присутствуют `sheet_reconciliation_runs`, `sheet_reconciliation_items` и 4 progress-поля.
- Миграция сохранила v1.0.18 FIFO без повторного repair: `queue_repair.action = already_applied`.
- Проверки: 190 Python-тестов и 24 Node-теста прошли.

## Реализовано

- Канонический universe: все `status <> deleted`; published: только `status = approved`.
- Добавлены `Без проекта`, `Без даты`, автоматические `YYYY-MM` и базовые вкладки 2026-05...2026-08.
- Добавлены derived-поля `publish_month`, `is_published`, `is_incomplete`, `missing_fields`.
- Реализованы read-only audit, safe backfill classification, durable staging rebuild, resume и final validation.
- Добавлены `Month Stats`, `Unfinished Requests`, `Unsubmitted Forms`, `Reconciliation`, `Project Backfill Review`.
- Переработаны `Project Stats` и `People × Projects`; опубликованными считаются только approved.
- Incremental sync обновляет `Videos`, project sheet и month sheet; managed values записываются как `RAW`.
- Добавлены `/sheets_audit`, `/reconcile_sheets`, `/sheets_status`, `/unfinished_requests`.

## Read-only аудит до mutation

Reconciliation run: `#1`, status `awaiting_confirmation`, stage `audit_done`.

| Метрика | Значение |
|---|---:|
| DB active | 310 |
| DB published / approved | 308 |
| DB pending | 0 |
| DB needs_revision | 2 |
| DB duplicate | 0 |
| Videos rows / unique IDs | 307 |
| Project union unique IDs | 278 |
| Month union unique IDs | 0 |
| Без проекта | 52 |
| Без даты | 2 |
| Safe project backfill candidates | 20 |
| Conflicting project assignments | 1 |
| Unfinished requests | 52 |
| Stale sessions | 0 |
| Total mismatch count | 766 |

### Детали расхождений

| Проверка | Значение |
|---|---:|
| Missing from Videos | 3 |
| Extra in Videos | 0 |
| Duplicate in Videos | 0 |
| Videos header mismatches | 1 |
| Videos row mismatches | 54 |
| Missing from project sheets | 32 |
| Duplicate in project sheets | 0 |
| Project membership mismatches | 53 |
| Project sheet-only IDs | 0 |
| Missing from month sheets | 310 |
| Duplicate in month sheets | 0 |
| Month membership mismatches | 310 |
| Month sheet-only IDs | 0 |
| Statistics mismatches | 3 |

## Месяцы до rebuild

| Период | DB active | DB published | Sheet rows |
|---|---:|---:|---:|
| 2026-05 | 0 | 0 | 0 |
| 2026-06 | 146 | 146 | 0 |
| 2026-07 | 162 | 162 | 0 |
| 2026-08 | 0 | 0 | 0 |
| Без даты | 2 | 0 | 0 |

`Весь Спорт`: DB active `257`, текущая вкладка `278`.

## Mutation gate

- Safe backfill **не применялся**.
- Изменённые project IDs: **нет**.
- Managed sheets **не пересобирались**.
- Массовые напоминания не отправлялись.
- `/return_missing_dates` не запускался.
- Неизвестные пользовательские вкладки не изменялись.

Для продолжения нужен выбор суперадмина:

1. `safe_backfill` — применить 20 однозначных назначений и пересобрать managed sheets.
2. `db_only` — ничего не backfill-ить и пересобрать строго из текущей БД.
3. Отмена.

## Production health

- FIFO: pending `0`, active pointer отсутствует, stale metadata `0`.
- Worker: `healthy`, `idle`; audit job завершён, ready/processing jobs `0`.
- Sheets sync: queued `0`, failed videos `0`.
- `WORK_CHAT_ID`: **в текущем Production deployment всё ещё присутствует**.

Перед финальным PASS необходимо удалить `WORK_CHAT_ID` именно из Vercel Production environment и выполнить новый production deployment. Финальный успех пока не заявлен: равенство `310 = Videos = project union = month union` ещё не достигнуто.
