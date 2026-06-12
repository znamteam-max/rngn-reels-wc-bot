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
    const speedPresets = { slow: 60, normal: 100, fast: 130 };

    function configuredSpeed(fallback) {
      const value = params.get('ticker') || params.get('speed');
      if (value && speedPresets[value]) return speedPresets[value];
      const number = Number(value || fallback);
      return Number.isFinite(number) ? Math.min(Math.max(number, 12), 220) : fallback;
    }

    let speed = configuredSpeed(serverConfig.speed);
    let refreshMs = Math.max(15000, Number(params.get('refresh') || serverConfig.refreshSeconds * 1000));
    const clientLimit = Math.min(Math.max(Number(params.get('limit') || serverConfig.limit || 100), 1), Number(serverConfig.maxLimit || 100));
    let activeText = track.textContent;
    let queuedText = '';
    let started = false;
    let inFlight = null;
    const diagnostics = {
      newsCount: Number(serverConfig.initialCount || 0),
      updatedAt: serverConfig.updatedAt || 'unknown',
      backgroundLoaded: serverConfig.backgroundUrl ? 'checking' : 'n/a',
      fontLoaded: serverConfig.fontFamily ? 'checking' : 'n/a',
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
      const titles = items.slice(0, clientLimit).map((item) => String(item && item.title || '').trim()).filter(Boolean);
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
            speed = configuredSpeed(data.state.speed || speed);
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
    const transparent = params.get('transparent') === '1';
    document.documentElement.classList.toggle('transparent-mode', transparent);
    document.body.classList.toggle('transparent', transparent);
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
      : Promise.resolve();

    fontReady.finally(() => {
      updateDiagnostics();
      restart(activeText);
    });
    refreshNews();
    setInterval(refreshNews, refreshMs);
  </script>`;
}

function renderSharedStageStyles() {
  return `
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
    html.transparent-mode body,
    body.transparent { background: transparent; }
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
      overflow: hidden;
      display: flex;
      align-items: center;
      pointer-events: none;
    }
    .ticker-track {
      position: absolute;
      left: 0;
      display: inline-flex;
      align-items: center;
      gap: 28px;
      line-height: 1;
      text-transform: uppercase;
      white-space: nowrap;
      will-change: transform;
    }
    @keyframes ticker-scroll {
      from { transform: translateX(var(--ticker-start, 85vw)); }
      to { transform: translateX(-100%); }
    }
  `;
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
    ${renderSharedStageStyles()}
    .football .ticker-mask {
      left: 110px;
      right: 36px;
      bottom: 3px;
      height: 48px;
      z-index: 1;
    }
    .football .ticker-track {
      display: inline-block;
      color: #f2f2f2;
      font-family: 'PFDinTextCompPro-BoldItal', sans-serif;
      font-size: 29px;
      font-weight: 700;
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
  <main class="ticker-stage ticker-panel football" aria-label="Новости чемпионата мира">
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
    limit: 20,
    maxLimit: 20,
    updatedAt: metadata.updatedAt,
    backgroundUrl: '/assets/football-ticker-bg.png',
    fontFamily: 'PFDinTextCompPro-BoldItal',
    fontProbeSize: 29,
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

export function renderTennisTicker(items, state, options = {}) {
  const size = options.size === 'small' ? 'small' : 'normal';
  const small = size === 'small';
  const asset = small ? '/assets/tennis-ticker-small.png' : '/assets/tennis-ticker-normal.png';
  const fontSize = small ? 31 : 42;
  const fallbackText = 'НОВОСТИ ТЕННИСА ВРЕМЕННО НЕДОСТУПНЫ';
  const limit = Math.min(Math.max(Number(options.limit || 15), 1), 15);
  const initialText = items.slice(0, limit).map((item) => escapeHtml(item.title)).join('   •   ')
    || fallbackText;

  return `<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Tennis ticker ${size}</title>
  <style>
    ${renderSharedStageStyles()}
    .tennis .ticker-mask {
      left: 220px;
      right: 40px;
      bottom: ${small ? 8 : 18}px;
      height: ${small ? 42 : 60}px;
      -webkit-mask-image: linear-gradient(to right, transparent 0, #000 ${small ? 28 : 44}px, #000 calc(100% - 80px), transparent 100%);
      mask-image: linear-gradient(to right, transparent 0, #000 ${small ? 28 : 44}px, #000 calc(100% - 80px), transparent 100%);
    }
    .tennis .ticker-track {
      color: #fff;
      font-family: 'PFDinTextCompPro-BoldItal', Arial, sans-serif;
      font-size: ${fontSize}px;
      font-style: italic;
      font-weight: 700;
      text-shadow: 0 2px 2px rgba(0, 0, 0, .24);
    }
  </style>
</head>
<body class="tennis-${size}">
  <main class="ticker-stage ticker-panel tennis tennis-${size}" aria-label="Новости тенниса">
    <img class="ticker-bg" src="${asset}" alt="">
    <div class="ticker-mask news-viewport">
      <div class="ticker-track news-track" id="newsTrack">${initialText}</div>
    </div>
  </main>
  ${renderTickerClient({
    endpoint: '/api/news/tennis',
    fallbackText,
    backgroundUrl: asset,
    fontFamily: 'PFDinTextCompPro-BoldItal',
    fontProbeSize: fontSize,
    stageSelector: '.ticker-stage',
    stageWidth: 1920,
    stageHeight: 1080,
    initialCount: items.length,
    limit,
    maxLimit: 15,
    enabled: state.enabled,
    speed: options.speed || state.speed,
    refreshSeconds: options.refreshSeconds || state.refreshSeconds,
  })}
</body>
</html>`;
}
