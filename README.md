# Ticker Hub

Отдельный Cloudflare Worker для эфирных бегущих строк. В проект входят:

- футбольная строка ЧМ с 20 последними новостями Sports.ru;
- перенесённая теннисная строка в двух размерах;
- ручные новости, режимы `auto` / `manual` / `mixed`, скорость и включение/выключение;
- KV-кэш с последним успешным ответом и demo fallback;
- отдельный Telegram-бот управления;
- автоматическое обновление новостей открытой строкой каждые две минуты.

## Локальный запуск

```powershell
npm install
npm run verify
npm run dev
```

Основные URL:

```text
http://localhost:8787/api/health
http://localhost:8787/ticker/football.html
http://localhost:8787/ticker/football.html?debug=1
http://localhost:8787/ticker/tennis.html
http://localhost:8787/ticker/tennis.html?height=small
http://localhost:8787/api/news/football-world-cup
http://localhost:8787/api/news/tennis
```

Без KV локальный Worker использует память текущего isolate, поэтому ручные новости и state работают до перезапуска.

## Управляющий API

```text
GET    /api/ticker/state?sport=football
POST   /api/ticker/state
POST   /api/ticker/manual
DELETE /api/ticker/manual?id=...
POST   /api/ticker/clear
POST   /api/ticker/refresh
GET    /api/cron/refresh?secret=...
GET    /api/admin/env-check
POST   /api/telegram/setup
GET    /api/telegram/status
POST   /telegram/webhook
```

Если задан `TICKER_ADMIN_SECRET`, управляющие маршруты требуют заголовок:

```text
x-ticker-admin-secret: <secret>
# или совместимый alias:
x-admin-secret: <secret>
```

## Production

1. Создать KV: `npx wrangler kv namespace create TICKER_KV`.
2. Вставить полученный ID в `wrangler.toml` и раскомментировать `[[kv_namespaces]]`.
3. Добавить секреты:

```powershell
npx wrangler secret put TICKER_ADMIN_SECRET
npx wrangler secret put TICKER_CRON_SECRET
npx wrangler secret put TELEGRAM_BOT_TOKEN
npx wrangler secret put TELEGRAM_WEBHOOK_SECRET
npx wrangler secret put ADMIN_CHAT_ID
```

4. Указать настоящий `PUBLIC_BASE_URL` в `wrangler.toml`.
5. Выполнить `npm run deploy`.
6. Вызвать `POST /api/telegram/setup` с admin secret, чтобы зарегистрировать Telegram webhook.

Внешний cron вызывает `GET /api/cron/refresh?secret=<TICKER_CRON_SECRET>` без headers и обновляет football + tennis cache. Маршрут также принимает trailing slash и безопасные `HEAD`-проверки сервисов мониторинга.

Для vMix используйте Browser Source `1920x1080` и URL `/ticker/football.html`.
Футбольная страница использует исходный `/assets/football-ticker-bg.png` как единое изображение в координатах `1920x1080`; responsive масштабирует весь stage целиком. Текстовая маска: `left: 275px`, `right: 40px`, `bottom: 6px`, `height: 70px`. Диагностический overlay доступен через `?debug=1`; прозрачный фон для broadcast source можно включить через `?transparent=1`.

Теннисная строка перенесена в hub как две готовые PNG-подложки, без пересборки дизайна в CSS:

```text
/assets/tennis-ticker-normal.png
/assets/tennis-ticker-small.png
```

URL для vMix:

```text
/ticker/tennis.html?height=normal
/ticker/tennis.html?height=small
```

Поддерживаются старые параметры теннисной строки: `ticker`, `height`, `cta`, `mode`, `refresh`, `limit`. Параметры `cta` и `mode` принимаются для совместимости, но visual layout строится по готовым PNG.

Координаты текста:

```text
normal: left 220px, right 40px, bottom 18px, height 60px, font-size 42px
small:  left 220px, right 40px, bottom 8px,  height 42px, font-size 31px
```

## Архив для передачи

После каждого завершённого и закоммиченного обновления собрать один безопасный ZIP:

```powershell
npm run package:release
```

Архив появится в `releases/`. Он содержит зафиксированную версию проекта и не включает локальные secrets, `.git`, `node_modules` или служебные файлы.
