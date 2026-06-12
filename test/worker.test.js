import assert from 'node:assert/strict';
import test from 'node:test';

import worker from '../src/index.js';
import { fetchAutoNews, parseSportsNewsPage } from '../src/news.js';
import { renderFootballTicker, renderTennisTicker } from '../src/render.js';

function request(path, init) {
  return new Request(`https://ticker.test${path}`, init);
}

test('Sports.ru parser filters and deduplicates football news', () => {
  const html = `
    <a href="/football/1117247511-news-one.html"><span>Первая настоящая футбольная новость чемпионата мира</span></a>
    <a href="/football/1117247511-news-one.html">Первая настоящая футбольная новость чемпионата мира</a>
    <a href="/football/tags/world-cup/">Турнирная таблица чемпионата мира</a>
    <a href="/tennis/1117247512-wrong.html">Новость из другого вида спорта</a>
  `;
  const items = parseSportsNewsPage(html, 'football', '2026-06-12T00:00:00.000Z');
  assert.equal(items.length, 1);
  assert.equal(items[0].source, 'sports.ru');
});

test('source failure returns the last successful KV cache', async () => {
  const store = new Map();
  const env = {
    TICKER_KV: {
      get: async (key) => store.get(key) || null,
      put: async (key, value) => store.set(key, value),
    },
  };
  const sourceHtml = '<a href="/football/1117247511-news-one.html">Первая настоящая футбольная новость чемпионата мира</a>';
  const originalFetch = globalThis.fetch;

  try {
    globalThis.fetch = async () => new Response(sourceHtml);
    const fresh = await fetchAutoNews(env, 'football', { force: true });
    assert.equal(fresh.fromCache, false);

    globalThis.fetch = async () => new Response('', { status: 503 });
    const cached = await fetchAutoNews(env, 'football', { force: true });
    assert.equal(cached.fromCache, true);
    assert.match(cached.warning, /using_cache/);
    assert.equal(cached.items[0].title, fresh.items[0].title);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('health endpoint responds', async () => {
  const response = await worker.fetch(request('/api/health'), {});
  const body = await response.json();
  assert.equal(response.status, 200);
  assert.equal(body.ok, true);
  assert.deepEqual(body.sports, ['football', 'tennis']);
});

test('manual mode works without KV during local development', async () => {
  const stateResponse = await worker.fetch(request('/api/ticker/state', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ sport: 'football', mode: 'manual' }),
  }), {});
  assert.equal(stateResponse.status, 200);

  const addResponse = await worker.fetch(request('/api/ticker/manual', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ sport: 'football', title: 'Тестовая ручная новость для футбольной строки' }),
  }), {});
  assert.equal(addResponse.status, 201);

  const newsResponse = await worker.fetch(request('/api/news/football-world-cup'), {});
  const news = await newsResponse.json();
  assert.equal(news.count, 1);
  assert.equal(news.items[0].source, 'manual');
});

test('admin secret protects mutations when configured', async () => {
  const response = await worker.fetch(request('/api/ticker/clear', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ sport: 'football' }),
  }), { TICKER_ADMIN_SECRET: 'secret' });
  assert.equal(response.status, 401);
});

test('admin env check accepts X-Admin-Secret alias and returns booleans only', async () => {
  const response = await worker.fetch(request('/api/admin/env-check', {
    headers: { 'x-admin-secret': 'secret' },
  }), {
    TICKER_ADMIN_SECRET: 'secret',
    TICKER_CRON_SECRET: 'cron-secret',
    TELEGRAM_BOT_TOKEN: 'telegram-token',
    TELEGRAM_WEBHOOK_SECRET: 'webhook-secret',
    TICKER_KV: {},
  });
  const body = await response.json();
  assert.equal(response.status, 200);
  assert.deepEqual(body, {
    ok: true,
    hasTickerAdminSecret: true,
    hasTickerCronSecret: true,
    hasTelegramBotToken: true,
    hasTelegramWebhookSecret: true,
    hasKv: true,
  });
  assert.equal(JSON.stringify(body).includes('secret'), false);
});

test('cron refresh rejects a wrong secret', async () => {
  const response = await worker.fetch(request('/api/cron/refresh?secret=wrong'), {
    TICKER_CRON_SECRET: 'correct',
  });
  assert.equal(response.status, 401);
});

test('cron refresh accepts a trailing slash and HEAD probes', async () => {
  const trailingSlash = await worker.fetch(request('/api/cron/refresh/?secret=wrong'), {
    TICKER_CRON_SECRET: 'correct',
  });
  const head = await worker.fetch(request('/api/cron/refresh?secret=wrong', { method: 'HEAD' }), {
    TICKER_CRON_SECRET: 'correct',
  });
  assert.equal(trailingSlash.status, 401);
  assert.equal(head.status, 401);
  assert.equal(await head.text(), '');
});

test('cron refresh updates football and tennis sources', async () => {
  const originalFetch = globalThis.fetch;
  try {
    globalThis.fetch = async (input) => {
      const url = String(input);
      const sport = url.includes('/tennis/') ? 'tennis' : 'football';
      return new Response(
        `<a href="/${sport}/1117247511-news-one.html">Первая настоящая новость для проверки cron endpoint</a>`,
      );
    };

    const response = await worker.fetch(request('/api/cron/refresh?secret=correct'), {
      TICKER_CRON_SECRET: 'correct',
    });
    const body = await response.json();
    assert.equal(response.status, 200);
    assert.equal(body.ok, true);
    assert.equal(body.source, 'cron');
    assert.equal(body.footballCount, 1);
    assert.equal(body.tennisCount, 1);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('football ticker includes the supplied font and background', async () => {
  const response = await worker.fetch(request('/ticker/football.html'), {});
  const body = await response.text();
  assert.equal(response.status, 200);
  assert.match(body, /PFDinTextCompPro-BoldItal\.ttf/);
  assert.match(body, /football-ticker-bg\.png/);
  assert.match(body, /<img class="ticker-bg" src="\/assets\/football-ticker-bg\.png"/);
  assert.match(body, /\.ticker-stage \{/);
  assert.match(body, /width: 1920px/);
  assert.match(body, /height: 1080px/);
  assert.match(body, /\.ticker-mask \{/);
  assert.match(body, /left: 0/);
  assert.match(body, /right: 0/);
  assert.match(body, /bottom: 3px/);
  assert.match(body, /height: 48px/);
  assert.match(body, /z-index: 1/);
  assert.match(body, /transparent 170px, #000 255px/);
  assert.match(body, /display: inline-block/);
  assert.match(body, /font-size: 29px/);
  assert.match(body, /width: 100%/);
  assert.match(body, /height: 100%/);
  assert.match(body, /background: #000/);
});

test('tennis ticker uses ready-made normal and small image assets', async () => {
  const normal = await worker.fetch(request('/ticker/tennis.html?height=normal&ticker=100&cta=0&mode=flex&refresh=60000&limit=15'), {});
  const small = await worker.fetch(request('/ticker/tennis.html?height=small'), {});
  const normalBody = await normal.text();
  const smallBody = await small.text();

  assert.equal(normal.status, 200);
  assert.equal(small.status, 200);
  assert.match(normalBody, /<img class="ticker-bg" src="\/assets\/tennis-ticker-normal\.png"/);
  assert.match(smallBody, /<img class="ticker-bg" src="\/assets\/tennis-ticker-small\.png"/);
  assert.doesNotMatch(normalBody, /background: url\('\/assets\/tennis/);
  assert.match(normalBody, /left: 220px/);
  assert.match(normalBody, /bottom: 18px/);
  assert.match(normalBody, /height: 60px/);
  assert.match(smallBody, /bottom: 8px/);
  assert.match(smallBody, /height: 42px/);
  assert.match(smallBody, /font-size: 31px/);
});

test('tennis ticker renderer respects the requested limit', () => {
  const items = Array.from({ length: 3 }, (_, index) => ({ title: `News ${index + 1}` }));
  const body = renderTennisTicker(items, {
    enabled: true,
    speed: 100,
    refreshSeconds: 120,
  }, {
    size: 'normal',
    limit: 2,
  });

  assert.match(body, /News 1/);
  assert.match(body, /News 2/);
  assert.doesNotMatch(body, /News 3/);
});

test('football ticker has a visible fallback and debug diagnostics', () => {
  const body = renderFootballTicker([], {
    enabled: true,
    speed: 70,
    refreshSeconds: 120,
  }, {
    buildVersion: 'test-build',
    debug: true,
  });

  assert.match(body, /НОВОСТИ ЧМ ЗАГРУЖАЮТСЯ/);
  assert.match(body, /class="debug-mode"/);
  assert.match(body, /buildVersion:/);
  assert.match(body, /loaded news count:/);
  assert.match(body, /background asset URL:/);
  assert.match(body, /background loaded:/);
  assert.match(body, /font loaded:/);
  assert.match(body, /last JS error:/);
});

test('football ticker supports transparent vMix mode', async () => {
  const response = await worker.fetch(request('/ticker/football.html?transparent=1'), {});
  const body = await response.text();

  assert.equal(response.status, 200);
  assert.match(body, /document\.body\.classList\.toggle\('transparent', transparent\)/);
  assert.match(body, /body\.transparent \{ background: transparent; \}/);
  assert.match(body, /\.ticker-stage \{/);
  assert.match(body, /background: transparent/);
});

test('football ticker starts outside the stage and clears the protected logo zone', () => {
  const body = renderFootballTicker([{ title: 'Test football headline' }], {
    enabled: true,
    speed: 70,
    refreshSeconds: 120,
  }, {
    buildVersion: 'test-build',
  });

  assert.match(body, /"startOutsideStage":true/);
  assert.match(body, /"startPadding":80/);
  assert.match(body, /"endPadding":320/);
  assert.match(body, /stageWidth \+ Number\(serverConfig\.startPadding \|\| 80\)/);
  assert.match(body, /-\(trackWidth \+ Number\(serverConfig\.endPadding \|\| 0\)\)/);
  assert.match(body, /--ticker-end/);
});

test('Telegram football links separate transparent vMix and preview URLs', async () => {
  const originalFetch = globalThis.fetch;
  const telegramCalls = [];

  try {
    globalThis.fetch = async (input, init = {}) => {
      if (String(input).includes('api.telegram.org')) {
        telegramCalls.push(JSON.parse(init.body));
        return new Response(JSON.stringify({ ok: true, result: true }), {
          headers: { 'content-type': 'application/json' },
        });
      }
      return new Response('', { status: 404 });
    };

    const env = {
      TELEGRAM_BOT_TOKEN: 'test-token',
      PUBLIC_BASE_URL: 'https://bolshe-ticker-hub.znamteam-903.workers.dev',
    };
    const callback = (data) => request('/telegram/webhook', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({
        callback_query: {
          id: `callback-${data}`,
          data,
          message: { message_id: 1, chat: { id: 1 } },
        },
      }),
    });

    await worker.fetch(callback('football:url'), env);
    assert.equal(
      telegramCalls.find((call) => call.text)?.text,
      'https://bolshe-ticker-hub.znamteam-903.workers.dev/ticker/football.html?transparent=1',
    );

    telegramCalls.length = 0;
    await worker.fetch(callback('football:preview'), env);
    assert.equal(
      telegramCalls.find((call) => call.text)?.text,
      'https://bolshe-ticker-hub.znamteam-903.workers.dev/ticker/football.html',
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});
