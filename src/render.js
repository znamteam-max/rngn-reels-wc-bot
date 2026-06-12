function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
}

function renderTickerClient(config) {
  return `<script>
    const serverConfig = ${JSON.stringify(config)};
    const params = new URLSearchParams(location.search);
    const endpoint = serverConfig.endpoint;
    const track = document.getElementById('newsTrack');
    const viewport = document.querySelector('.news-viewport');
    const panel = document.querySelector('.ticker-panel');
    let speed = Math.min(Math.max(Number(params.get('speed') || serverConfig.speed), 12), 220);
    let refreshMs = Math.max(15000, Number(params.get('refresh') || serverConfig.refreshSeconds * 1000));
    let activeText = track.textContent;
    let queuedText = '';
    let started = false;
    let inFlight = null;

    function setEnabled(enabled) {
      panel.hidden = enabled === false;
    }

    function restart(text) {
      activeText = text || 'Новости временно недоступны';
      track.style.animation = 'none';
      track.textContent = activeText;
      const distance = (viewport.clientWidth || 1600) + (track.scrollWidth || 1200);
      track.style.setProperty('--ticker-start', (viewport.clientWidth || 1600) + 'px');
      track.style.setProperty('--ticker-duration', Math.max(12, Math.round(distance / speed)) + 's');
      void track.offsetWidth;
      track.style.animation = 'ticker-scroll var(--ticker-duration) linear 1';
      started = true;
    }

    function textFromItems(items) {
      const titles = items.map((item) => String(item && item.title || '').trim()).filter(Boolean);
      return titles.join('   •   ') || 'Новости временно недоступны';
    }

    async function refreshNews() {
      if (inFlight) return inFlight;
      inFlight = fetch(endpoint + '?ts=' + Date.now(), { cache: 'no-store' })
        .then((response) => {
          if (!response.ok) throw new Error(String(response.status));
          return response.json();
        })
        .then((data) => {
          queuedText = textFromItems(Array.isArray(data.items) ? data.items : []);
          if (data.state) {
            speed = Math.min(Math.max(Number(params.get('speed') || data.state.speed || speed), 12), 220);
            refreshMs = Math.max(15000, Number(params.get('refresh') || data.state.refreshSeconds * 1000 || refreshMs));
            setEnabled(data.state.enabled);
          }
          if (!started) {
            restart(queuedText);
            queuedText = '';
          }
        })
        .catch(() => {})
        .finally(() => {
          inFlight = null;
        });
      return inFlight;
    }

    track.addEventListener('animationend', () => {
      restart(queuedText || activeText);
      queuedText = '';
      refreshNews();
    });

    window.addEventListener('resize', () => restart(activeText));
    setEnabled(serverConfig.enabled);
    Promise.resolve(document.fonts && document.fonts.ready).finally(() => restart(activeText));
    refreshNews();
    setInterval(refreshNews, refreshMs);
  </script>`;
}

export function renderFootballTicker(items, state) {
  const initialText = items.map((item) => escapeHtml(item.title)).join('   •   ')
    || 'Новости чемпионата мира появятся здесь';

  return `<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=1920, initial-scale=1">
  <title>Football World Cup ticker</title>
  <style>
    @font-face {
      font-family: 'PFDinTextCompPro-BoldItal';
      src: url('/fonts/PFDinTextCompPro-BoldItal.ttf') format('truetype');
      font-weight: 700;
      font-style: italic;
      font-display: swap;
    }
    * { box-sizing: border-box; }
    html, body {
      margin: 0;
      width: 1920px;
      height: 1080px;
      overflow: hidden;
      background: transparent;
    }
    .frame {
      position: relative;
      width: 1920px;
      height: 1080px;
      overflow: hidden;
      background: url('/assets/football-ticker-bg.png') center / 1920px 1080px no-repeat;
    }
    .ticker-panel[hidden] { display: none; }
    .news-viewport {
      position: absolute;
      left: 260px;
      right: 32px;
      bottom: 20px;
      height: 76px;
      overflow: hidden;
      display: flex;
      align-items: center;
      -webkit-mask-image: linear-gradient(90deg, transparent 0, #000 100px, #000 100%);
      mask-image: linear-gradient(90deg, transparent 0, #000 100px, #000 100%);
    }
    .news-track {
      position: absolute;
      left: 0;
      display: inline-block;
      color: #fff;
      font-family: 'PFDinTextCompPro-BoldItal', 'Arial Narrow', Arial, sans-serif;
      font-size: 40px;
      font-style: italic;
      font-weight: 700;
      letter-spacing: .02em;
      line-height: 1;
      text-shadow: 0 2px 6px rgba(0, 0, 0, .55);
      text-transform: uppercase;
      white-space: nowrap;
      will-change: transform;
    }
    @keyframes ticker-scroll {
      from { transform: translateX(var(--ticker-start, 1628px)); }
      to { transform: translateX(-100%); }
    }
  </style>
</head>
<body>
  <main class="frame">
    <section class="ticker-panel" aria-label="Новости чемпионата мира">
      <div class="news-viewport"><div class="news-track" id="newsTrack">${initialText}</div></div>
    </section>
  </main>
  ${renderTickerClient({
    endpoint: '/api/news/football-world-cup',
    enabled: state.enabled,
    speed: state.speed,
    refreshSeconds: state.refreshSeconds,
  })}
</body>
</html>`;
}

export function renderTennisTicker(items, state, size = 'normal') {
  const small = size === 'small';
  const height = small ? 51 : 102;
  const initialText = items.map((item) => escapeHtml(item.title)).join('   ✦   ')
    || 'Новости тенниса временно недоступны';

  return `<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=1920, initial-scale=1">
  <title>Tennis ticker</title>
  <style>
    * { box-sizing: border-box; }
    html, body {
      margin: 0;
      width: 1920px;
      height: 1080px;
      overflow: hidden;
      background: transparent;
      font-family: Arial, sans-serif;
    }
    .frame { position: relative; width: 1920px; height: 1080px; overflow: hidden; }
    .ticker-panel {
      position: absolute;
      left: 0;
      right: 0;
      bottom: 0;
      height: ${height}px;
      overflow: hidden;
      background: url('/assets/${small ? 'tennis-ticker-bg-small.png' : 'tennis-ticker-bg.png'}') left ${small ? 'top' : 'bottom'} / 1920px 1080px no-repeat;
    }
    .ticker-panel[hidden] { display: none; }
    .news-viewport {
      position: absolute;
      left: ${small ? 72 : 150}px;
      right: 0;
      top: 0;
      height: ${height}px;
      overflow: hidden;
      display: flex;
      align-items: center;
      -webkit-mask-image: linear-gradient(90deg, transparent 0, #000 ${small ? 56 : 112}px, #000 100%);
      mask-image: linear-gradient(90deg, transparent 0, #000 ${small ? 56 : 112}px, #000 100%);
    }
    .news-track {
      position: absolute;
      left: 0;
      display: inline-block;
      color: #fff;
      font-family: Arial, sans-serif;
      font-size: ${small ? 31 : 42}px;
      font-style: italic;
      font-weight: 900;
      line-height: 1;
      text-shadow: 0 2px 2px rgba(0, 0, 0, .24);
      text-transform: uppercase;
      white-space: nowrap;
      will-change: transform;
    }
    @keyframes ticker-scroll {
      from { transform: translateX(var(--ticker-start, 1770px)); }
      to { transform: translateX(-100%); }
    }
  </style>
</head>
<body>
  <main class="frame">
    <section class="ticker-panel" aria-label="Новости тенниса">
      <div class="news-viewport"><div class="news-track" id="newsTrack">${initialText}</div></div>
    </section>
  </main>
  ${renderTickerClient({
    endpoint: '/api/news/tennis',
    enabled: state.enabled,
    speed: state.speed,
    refreshSeconds: state.refreshSeconds,
  })}
</body>
</html>`;
}
