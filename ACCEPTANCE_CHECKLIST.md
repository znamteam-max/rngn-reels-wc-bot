# Acceptance checklist

## Local

- [ ] `npm install` работает.
- [ ] `npm run dev` запускает Worker locally.
- [ ] `/api/health` -> ok.
- [ ] `/ticker/football.html` -> HTTP 200.
- [ ] `/api/news/football-world-cup` -> JSON.
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
- [ ] vMix Browser Source показывает строку.
- [ ] Финальная PNG-плашка подключается без изменения логики.
