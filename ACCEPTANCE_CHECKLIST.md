# Acceptance checklist

## Local

- [ ] `npm install` работает.
- [ ] `npm run dev` запускает Worker locally.
- [ ] `/api/health` -> ok.
- [ ] `/ticker/football.html` -> HTTP 200.
- [ ] `/ticker/tennis.html?height=normal` -> HTTP 200.
- [ ] `/ticker/tennis.html?height=small` -> HTTP 200.
- [ ] `/api/news/football-world-cup` -> JSON.
- [ ] `/api/news/tennis` -> 15 items.
- [ ] Manual news можно добавить через API.
- [ ] Manual news появляется в ticker.

## Telegram

- [ ] `/start` показывает выбор спорта.
- [ ] Football menu открывается.
- [ ] Bot выдаёт vMix URL.
- [ ] Bot добавляет ручную новость.
- [ ] Bot может очистить очередь.
- [ ] Bot может обновить auto news.

## Production

- [ ] Cloudflare deploy проходит.
- [ ] Production `/api/health` ok.
- [ ] Production `/ticker/football.html` открывается без авторизации.
- [ ] Production `/ticker/tennis.html?height=normal` открывается без авторизации.
- [ ] Production `/ticker/tennis.html?height=small` открывается без авторизации.
- [ ] `GET /api/cron/refresh?secret=wrong` -> HTTP 401.
- [ ] `GET /api/cron/refresh?secret=<TICKER_CRON_SECRET>` -> HTTP 200.
- [ ] vMix Browser Source показывает строку.
- [ ] Финальная PNG-плашка подключается без изменения логики.
