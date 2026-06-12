# Архитектура

## Вариант MVP

Cloudflare Worker + KV.

Worker routes:

```text
GET  /api/health
GET  /ticker/football.html
GET  /ticker/tennis.html
GET  /api/news/football-world-cup
GET  /api/ticker/state?sport=football
POST /api/ticker/state
POST /api/ticker/manual
POST /api/ticker/clear
POST /api/ticker/refresh
POST /telegram/webhook
```

KV keys:

```text
ticker:football:state
ticker:football:manual
ticker:football:cache
ticker:tennis:state
ticker:tennis:cache
telegram:sessions:<chatId>
```

## News pipeline

```text
sources -> normalize -> dedupe -> moderate/approve -> cache -> ticker HTML
```

## Render

vMix открывает `/ticker/football.html` как Browser Source.
Страница сама опрашивает `/api/news/football-world-cup` каждые N секунд.
