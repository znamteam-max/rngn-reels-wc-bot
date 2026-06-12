# Файлы, которые нужно дополнительно приложить из старого проекта

Из локального проекта:

```text
C:\Users\znambo\Documents\New project\TENNIS-Listen-Bolshe
```

желательно приложить:

```text
cloudflare/overlay-worker/src/index.js
cloudflare/overlay-worker/wrangler.toml
cloudflare/overlay-worker/package.json
cloudflare/overlay-worker/package-lock.json
cloudflare/overlay-worker/public/
LISTEN_BOLSHE_LIVE_HISTORY.md
```

Зачем:
- Codex сможет найти текущую реализацию `/news-ticker.html`;
- перенести tennis ticker в новый проект;
- не трогать match overlay/Winline.
