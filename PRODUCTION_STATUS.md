# Production status

Deployed Worker:

```text
https://bolshe-ticker-hub.znamteam-903.workers.dev
```

Ready:

- football ticker at `/ticker/football.html`;
- transparent football vMix ticker at `/ticker/football.html?transparent=1`;
- responsive football layout with diagnostics at `/ticker/football.html?debug=1`;
- tennis ticker at `/ticker/tennis.html?height=normal` and `/ticker/tennis.html?height=small`;
- live Sports.ru parsing: 20 football World Cup items and 15 tennis items;
- production KV cache/state/manual-news storage;
- protected admin API;
- external cron endpoint protected by a separate secret;
- protected Telegram webhook and complete bot menu logic;
- supplied `PFDinTextCompPro-BoldItal` font;
- local tests, HTTP checks and 1920x1080 browser verification.

Football visual:

```text
asset: public/assets/football-ticker-bg.png
visible plate: bottom 54px of the 1920x1080 transparent PNG
mask: left 110px, right 36px, bottom 3px, height 48px
font-size: 29px
vMix URL: /ticker/football.html?transparent=1
```

Tennis visual assets:

```text
public/assets/tennis-ticker-normal.png
public/assets/tennis-ticker-small.png
```

Tennis text coordinates:

```text
normal: left 220px, right 40px, bottom 18px, height 60px, font-size 42px
small:  left 220px, right 40px, bottom 8px,  height 42px, font-size 31px
```

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
