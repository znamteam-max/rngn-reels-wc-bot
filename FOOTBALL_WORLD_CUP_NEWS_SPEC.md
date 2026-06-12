# Football World Cup news source spec

## Main source

```text
https://www.sports.ru/football/tournament/fifa-world-cup/news/
```

## API

```text
GET /api/news/football-world-cup
```

Response:

```json
{
  "ok": true,
  "sport": "football",
  "competition": "world-cup",
  "sourceUrl": "https://www.sports.ru/football/tournament/fifa-world-cup/news/",
  "count": 20,
  "items": [
    {
      "id": "sportsru-...",
      "title": "...",
      "url": "https://www.sports.ru/...",
      "source": "sports.ru",
      "sourceName": "Sports.ru",
      "createdAt": "...",
      "approved": true
    }
  ],
  "updatedAt": "..."
}
```

## Fetch/parsing requirements

- fetch with normal browser-like `User-Agent`;
- parse links from the news page;
- keep only real news links/titles;
- remove duplicates;
- return latest 20;
- cache successful result in KV;
- if source fetch fails, return last cached result;
- if cache is empty, return demo fallback and expose `warning`.

## Manual override

Keep manual news support for emergency, but auto Sports.ru source is primary.
Manual news can be prepended or pinned later.
