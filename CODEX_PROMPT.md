# Задача для Codex: отдельный проект бегущих строк

Создай отдельный проект для бегущих строк: `ticker-hub`.

Не трогать текущий проект Listen Больше / TENNIS-Listen-Bolshe:
- не трогать матчевый overlay;
- не трогать Winline;
- не трогать odds-service;
- не трогать текущий Telegram-бот матча.

Нужен отдельный сервис только для бегущих строк.

## Контекст

В старом проекте уже есть теннисная бегущая строка:
- `/news-ticker.html`;
- два размера;
- используется в эфире Listen Больше.

Её можно использовать как референс и/или перенести в новый проект.

## Новая задача

Сделать футбольную бегущую строку для чемпионата мира.

Источник новостей:

```text
https://www.sports.ru/football/tournament/fifa-world-cup/news/
```

Требования:
- брать 20 последних новостей;
- выводить их в бегущей строке по кругу;
- новые новости должны подтягиваться после обновления API;
- текст должен уходить в градиент перед левым лого/лейблом;
- фон брать из `public/assets/football-ticker-bg.png`;
- шрифт использовать `PFDinTextCompPro-BoldItal`;
- один размер: Browser Source 1920×1080, текст в нижней плашке.

## Font

TTF-файл не включён в архив. Положить вручную:

```text
public/fonts/PFDinTextCompPro-BoldItal.ttf
```

## Routes

Cloudflare Worker отдельным проектом:

```text
GET  /api/health
GET  /ticker/football.html
GET  /api/news/football-world-cup
POST /api/ticker/refresh
POST /api/ticker/manual
POST /api/ticker/clear
POST /telegram/webhook
```

Tennis позже можно перенести в:

```text
GET /ticker/tennis.html
```

## Acceptance

1. `/ticker/football.html` открывается как прозрачный/полный 1920×1080 browser source.
2. Видна присланная плашка.
3. Текст идёт только внутри нижней строки.
4. Текст перед левым лого исчезает через mask/gradient.
5. `/api/news/football-world-cup` возвращает 20 новостей Sports.ru.
6. При ошибке Sports.ru API отдаёт последний cache.
7. Telegram bot можно добавить позже, но структура для него уже заложена.
