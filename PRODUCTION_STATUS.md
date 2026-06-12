# Production status

Deployed Worker:

```text
https://bolshe-ticker-hub.znamteam-903.workers.dev
```

Ready:

- football ticker at `/ticker/football.html`;
- tennis ticker at `/ticker/tennis.html` and `?height=small`;
- live Sports.ru parsing: 20 football World Cup items and 15 tennis items;
- production KV cache/state/manual-news storage;
- protected admin API;
- external cron endpoint protected by a separate secret;
- protected Telegram webhook and complete bot menu logic;
- supplied `PFDinTextCompPro-BoldItal` font;
- local tests, HTTP checks and 1920x1080 browser verification.

Secrets are stored in ignored local `.dev.vars` and in Cloudflare:

- `TICKER_ADMIN_SECRET`;
- `TICKER_CRON_SECRET`;
- `TELEGRAM_WEBHOOK_SECRET`.

Telegram:

- bot: `@rngn_running_stroka_bot`;
- webhook: `https://bolshe-ticker-hub.znamteam-903.workers.dev/telegram/webhook`;
- webhook registered successfully;
- pending updates: `0`;
- last webhook error: none.

Cloudflare account already uses all five cron trigger slots, so this Worker does not claim another one. External cron uses `GET /api/cron/refresh?secret=...`; the complete secret-bearing URL is stored locally in ignored `.cron-jobs-url.txt`. Open ticker pages also refresh their API data every 120 seconds.
