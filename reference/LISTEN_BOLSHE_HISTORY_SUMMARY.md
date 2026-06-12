# Listen Больше history summary for ticker task

Текущий проект: `TENNIS-Listen-Bolshe`.

Уже есть:
- Cloudflare Worker `tennis-listen-bolshe-overlay`;
- `/news-ticker.html` и связанные ticker routes;
- tennis ticker в двух размерах;
- match overlay и Winline odds flow — их НЕ трогать в новом проекте.

Последние важные статусы:
- Winline sidecar unified flow задеплоен.
- Node mirror MVP создан, но для ticker hub пока не нужен.
- Последняя правка по сохранению overlay URL/state задеплоена.

Для новой задачи нужен отдельный проект только под бегущие строки.
