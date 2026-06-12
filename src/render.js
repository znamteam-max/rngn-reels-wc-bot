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
    const fallbackText = serverConfig.fallbackText || 'НОВОСТИ ВРЕМЕННО НЕДОСТУПНЫ';
    const track = document.getElementById('newsTrack');
    const viewport = document.querySelector('.news-viewport');
    const panel = document.querySelector('.ticker-panel');
    const stage = serverConfig.stageSelector ? document.querySelector(serverConfig.stageSelector) : null;
    const debugPanel = document.getElementById('debugPanel');
    const debugEnabled = params.get('debug') === '1' && Boolean(debugPanel);
    let speed = Math.min(Math.max(Number(params.get('speed') || serverConfig.speed), 12), 220);
    let refreshMs = Math.max(15000, Number(params.get('refresh') || serverConfig.refreshSeconds * 1000));
    let activeText = track.textContent;
    let queuedText = '';
    let started = false;
    let inFlight = null;
    const diagnostics = {
      newsCount: Number(serverConfig.initialCount || 0),
      updatedAt: serverConfig.updatedAt || 'unknown',
      backgroundLoaded: 'checking',
      fontLoaded: 'checking',
      lastError: 'none',
    };

    function diagnosticValue(id, value) {
      const element = document.getElementById(id);
      if (element) element.textContent = String(value);
    }

    function updateDiagnostics() {
      if (!debugEnabled) return;
      diagnosticValue('debugBuildVersion', serverConfig.buildVersion || 'unknown');
      diagnosticValue('debugNewsCount', diagnostics.newsCount);
      diagnosticValue('debugUpdatedAt', diagnostics.updatedAt);
      diagnosticValue('debugBackgroundUrl', serverConfig.backgroundUrl || 'n/a');
      diagnosticValue('debugBackground', diagnostics.backgroundLoaded);
      diagnosticValue('debugFont', diagnostics.fontLoaded);
      diagnosticValue('debugEndpoint', endpoint);
      diagnosticValue('debugLastError', diagnostics.lastError);
    }

    function reportError(error) {
      diagnostics.lastError = String(error && (error.message || error.reason || error) || 'unknown error');
      updateDiagnostics();
    }

    window.addEventListener('error', (event) => {
      if (event.target && event.target !== window) {
        reportError('resource: ' + (event.target.currentSrc || event.target.src || event.target.href || event.target.tagName));
        return;
      }
      reportError(event.error || event.message);
    }, true);
    window.addEventListener('unhandledrejection', (event) => reportError(event.reason));

    function setEnabled(enabled) {
      panel.hidden = enabled === false;
    }

    function fitStage() {
      if (!stage) return;
      const baseWidth = Number(serverConfig.stageWidth || 1920);
      const baseHeight = Number(serverConfig.stageHeight || 1080);
      const scale = Math.min(window.innerWidth / baseWidth, window.innerHeight / baseHeight);
      const left = Math.round((window.innerWidth - baseWidth * scale) / 2);
      const top = Math.round((window.innerHeight - baseHeight * scale) / 2);
      stage.style.left = left + 'px';
      stage.style.top = top + 'px';
      stage.style.transform = 'scale(' + scale + ')';
    }

    function restart(text) {
      activeText = text || fallbackText;
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
      return titles.join('   •   ') || fallbackText;
    }

    async function refreshNews() {
      if (inFlight) return inFlight;
      inFlight = fetch(endpoint + '?ts=' + Date.now(), { cache: 'no-store' })
        .then((response) => {
          if (!response.ok) throw new Error(String(response.status));
          return response.json();
        })
        .then((data) => {
          const items = Array.isArray(data.items) ? data.items : [];
          queuedText = textFromItems(items);
          diagnostics.newsCount = items.length;
          diagnostics.updatedAt = data.updatedAt || diagnostics.updatedAt;
          if (data.state) {
            speed = Math.min(Math.max(Number(params.get('speed') || data.state.speed || speed), 12), 220);
            refreshMs = Math.max(15000, Number(params.get('refresh') || data.state.refreshSeconds * 1000 || refreshMs));
            setEnabled(data.state.enabled);
          }
          updateDiagnostics();
          if (!started) {
            restart(queuedText);
            queuedText = '';
          }
        })
        .catch((error) => {
          reportError('news API: ' + (error && error.message || error));
          if (!started) restart(fallbackText);
        })
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

    window.addEventListener('resize', () => {
      fitStage();
      restart(activeText);
    });
    if (params.get('transparent') === '1') {
      document.documentElement.classList.add('transparent-mode');
    }
    fitStage();
    setEnabled(serverConfig.enabled);
    updateDiagnostics();

    if (serverConfig.backgroundUrl) {
      const backgroundProbe = new Image();
      backgroundProbe.onload = () => {
        diagnostics.backgroundLoaded = 'yes';
        updateDiagnostics();
      };
      backgroundProbe.onerror = () => {
        diagnostics.backgroundLoaded = 'no';
        reportError('background failed: ' + serverConfig.backgroundUrl);
      };
      backgroundProbe.src = serverConfig.backgroundUrl + '?probe=' + encodeURIComponent(serverConfig.buildVersion || Date.now());
    } else {
      diagnostics.backgroundLoaded = 'n/a';
    }

    const fontReady = serverConfig.fontFamily && document.fonts
      ? document.fonts.load('700 ' + Number(serverConfig.fontProbeSize || 40) + 'px "' + serverConfig.fontFamily + '"')
        .then((fonts) => {
          diagnostics.fontLoaded = fonts.length > 0 ? 'yes' : 'no';
        })
        .catch((error) => {
          diagnostics.fontLoaded = 'no';
          reportError('font failed: ' + (error && error.message || error));
        })
      : Promise.resolve().then(() => {
        diagnostics.fontLoaded = serverConfig.fontFamily ? 'no' : 'n/a';
      });

    fontReady.finally(() => {
      updateDiagnostics();
      restart(activeText);
    });
    refreshNews();
    setInterval(refreshNews, refreshMs);
  </script>`;
}

export function renderFootballTicker(items, state, metadata = {}) {
  const fallbackText = 'НОВОСТИ ЧМ ЗАГРУЖАЮТСЯ';
  const initialText = items.map((item) => escapeHtml(item.title)).join('   •   ')
    || fallbackText;

  return `<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
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
      width: 100%;
      height: 100%;
      overflow: hidden;
      background: #000;
    }
    html.transparent-mode,
    html.transparent-mode body { background: transparent; }
    .ticker-stage {
      position: fixed;
      width: 1920px;
      height: 1080px;
      overflow: hidden;
      background: transparent;
      transform-origin: top left;
    }
    .ticker-bg {
      position: absolute;
      left: 0;
      bottom: 0;
      width: 1920px;
      height: auto;
      display: block;
      pointer-events: none;
      user-select: none;
    }
    .ticker-panel[hidden] { display: none; }
    .ticker-mask {
      position: absolute;
      left: 275px;
      right: 40px;
      bottom: 6px;
      height: 70px;
      overflow: hidden;
      display: flex;
      align-items: center;
      pointer-events: none;
      -webkit-mask-image: linear-gradient(to right, transparent 0, #000 35px, #000 calc(100% - 80px), transparent 100%);
      mask-image: linear-gradient(to right, transparent 0, #000 35px, #000 calc(100% - 80px), transparent 100%);
    }
    .ticker-track {
      position: absolute;
      left: 0;
      display: inline-flex;
      align-items: center;
      gap: 28px;
      color: #f2f2f2;
      font-family: 'PFDinTextCompPro-BoldItal', sans-serif;
      font-size: 34px;
      line-height: 1;
      text-transform: uppercase;
      white-space: nowrap;
      will-change: transform;
    }
    @keyframes ticker-scroll {
      from { transform: translateX(var(--ticker-start, 85vw)); }
      to { transform: translateX(-100%); }
    }
    .debug-panel {
      position: fixed;
      z-index: 20;
      top: 16px;
      left: 16px;
      display: none;
      width: min(620px, calc(100vw - 32px));
      padding: 14px 16px;
      border: 1px solid rgba(255, 106, 22, .9);
      border-radius: 8px;
      background: rgba(0, 0, 0, .88);
      color: #f5f7fb;
      font: 14px/1.45 Consolas, 'Courier New', monospace;
      overflow-wrap: anywhere;
    }
    .debug-panel strong { color: #ff6a16; }
    .debug-mode .debug-panel { display: block; }
  </style>
</head>
<body class="${metadata.debug ? 'debug-mode' : ''}">
  <main class="ticker-stage ticker-panel" aria-label="Новости чемпионата мира">
    <img class="ticker-bg" src="/assets/football-ticker-bg.png" alt="">
    <div class="ticker-mask news-viewport">
      <div class="ticker-track news-track" id="newsTrack">${initialText}</div>
    </div>
  </main>
  <aside class="debug-panel" id="debugPanel" aria-label="Ticker diagnostics">
    <div><strong>buildVersion:</strong> <span id="debugBuildVersion">checking</span></div>
    <div><strong>loaded news count:</strong> <span id="debugNewsCount">0</span></div>
    <div><strong>last updated:</strong> <span id="debugUpdatedAt">unknown</span></div>
    <div><strong>background asset URL:</strong> <span id="debugBackgroundUrl">checking</span></div>
    <div><strong>background loaded:</strong> <span id="debugBackground">checking</span></div>
    <div><strong>font loaded:</strong> <span id="debugFont">checking</span></div>
    <div><strong>current API endpoint:</strong> <span id="debugEndpoint">unknown</span></div>
    <div><strong>last JS error:</strong> <span id="debugLastError">none</span></div>
  </aside>
  ${renderTickerClient({
    endpoint: '/api/news/football-world-cup',
    fallbackText,
    buildVersion: metadata.buildVersion,
    initialCount: items.length,
    updatedAt: metadata.updatedAt,
    backgroundUrl: '/assets/football-ticker-bg.png',
    fontFamily: 'PFDinTextCompPro-BoldItal',
    fontProbeSize: 34,
    stageSelector: '.ticker-stage',
    stageWidth: 1920,
    stageHeight: 1080,
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
    || 'НОВОСТИ ТЕННИСА ВРЕМЕННО НЕДОСТУПНЫ';

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
    fallbackText: 'НОВОСТИ ТЕННИСА ВРЕМЕННО НЕДОСТУПНЫ',
    enabled: state.enabled,
    speed: state.speed,
    refreshSeconds: state.refreshSeconds,
  })}
</body>
</html>`;
}
