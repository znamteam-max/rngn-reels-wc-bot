import {
  ADMIN_SECRET_HEADER,
  ADMIN_SECRET_HEADER_ALIAS,
  BUILD_VERSION,
  SOURCE_CONFIG,
  SPORTS,
  isSport,
} from './config.js';
import {
  addManualNews,
  clearManualNews,
  deleteManualNews,
  fetchAutoNews,
  getTickerNews,
} from './news.js';
import { renderFootballTicker, renderTennisTicker } from './render.js';
import { getState, updateState } from './state.js';
import { getTelegramStatus, handleTelegramWebhook, setupTelegramWebhook } from './telegram.js';

function json(data, status = 200, extraHeaders = {}) {
  return new Response(JSON.stringify(data, null, 2), {
    status,
    headers: {
      'content-type': 'application/json; charset=utf-8',
      'cache-control': 'no-store',
      'access-control-allow-origin': '*',
      ...extraHeaders,
    },
  });
}

function html(body, status = 200) {
  return new Response(body, {
    status,
    headers: {
      'content-type': 'text/html; charset=utf-8',
      'cache-control': 'no-store',
    },
  });
}

function adminAuthorized(request, env) {
  if (!env.TICKER_ADMIN_SECRET) return true;
  const headerSecret = request.headers.get(ADMIN_SECRET_HEADER)
    || request.headers.get(ADMIN_SECRET_HEADER_ALIAS);
  const bearer = request.headers.get('authorization')?.replace(/^Bearer\s+/i, '');
  return headerSecret === env.TICKER_ADMIN_SECRET || bearer === env.TICKER_ADMIN_SECRET;
}

function requireAdmin(request, env) {
  return adminAuthorized(request, env)
    ? null
    : json({
      ok: false,
      error: 'unauthorized',
      expectedHeaders: [ADMIN_SECRET_HEADER, ADMIN_SECRET_HEADER_ALIAS],
    }, 401);
}

async function requestBody(request) {
  return request.json().catch(() => ({}));
}

function requestedSport(url, body = {}) {
  return String(body.sport || url.searchParams.get('sport') || 'football').toLowerCase();
}

async function newsResponse(env, sport, url) {
  const state = await getState(env, sport);
  const force = url.searchParams.get('force') === '1';
  const news = await getTickerNews(env, sport, state, { force });
  const config = SOURCE_CONFIG[sport];

  return {
    ok: true,
    sport,
    competition: config.competition,
    sourceUrl: config.sourceUrl,
    items: news.items,
    count: news.items.length,
    autoCount: news.auto?.items?.length || 0,
    manualCount: news.manual.length,
    updatedAt: news.auto?.updatedAt || new Date().toISOString(),
    fromCache: news.auto?.fromCache || false,
    warning: news.auto?.warning || null,
    state,
  };
}

async function handleTicker(request, env, sport) {
  const url = new URL(request.url);
  const state = await getState(env, sport);
  const news = await getTickerNews(env, sport, state);

  if (sport === 'football') {
    return html(renderFootballTicker(news.items, state, {
      buildVersion: BUILD_VERSION,
      updatedAt: news.auto?.updatedAt || null,
      debug: url.searchParams.get('debug') === '1',
    }));
  }
  const size = ['small', 'normal'].includes(url.searchParams.get('height'))
    ? url.searchParams.get('height')
    : 'normal';
  const limit = Math.min(Math.max(Number(url.searchParams.get('limit') || state.limit || 15), 1), 15);
  return html(renderTennisTicker(news.items, state, {
    size,
    limit,
  }));
}

async function handleState(request, env, url) {
  if (request.method === 'GET') {
    const sport = requestedSport(url);
    if (!isSport(sport)) return json({ ok: false, error: 'unsupported_sport' }, 400);
    return json({ ok: true, state: await getState(env, sport) });
  }

  const denied = requireAdmin(request, env);
  if (denied) return denied;
  const body = await requestBody(request);
  const sport = requestedSport(url, body);
  if (!isSport(sport)) return json({ ok: false, error: 'unsupported_sport' }, 400);
  return json({ ok: true, state: await updateState(env, sport, body) });
}

async function handleManual(request, env, url) {
  const denied = requireAdmin(request, env);
  if (denied) return denied;

  const body = await requestBody(request);
  const sport = requestedSport(url, body);
  if (!isSport(sport)) return json({ ok: false, error: 'unsupported_sport' }, 400);

  if (request.method === 'DELETE') {
    const id = String(body.id || url.searchParams.get('id') || '');
    if (!id) return json({ ok: false, error: 'missing_id' }, 400);
    return json({ ok: true, deleted: await deleteManualNews(env, sport, id) });
  }

  try {
    const result = await addManualNews(env, sport, body);
    return json({ ok: true, item: result.item, total: result.items.length }, 201);
  } catch (error) {
    return json({ ok: false, error: error.message }, 400);
  }
}

async function handleClear(request, env, url) {
  const denied = requireAdmin(request, env);
  if (denied) return denied;

  const body = await requestBody(request);
  const sport = requestedSport(url, body);
  if (!isSport(sport)) return json({ ok: false, error: 'unsupported_sport' }, 400);
  await clearManualNews(env, sport);
  return json({ ok: true, sport, cleared: true });
}

async function handleRefresh(request, env, url) {
  const denied = requireAdmin(request, env);
  if (denied) return denied;

  const body = await requestBody(request);
  const sport = requestedSport(url, body);
  if (!isSport(sport)) return json({ ok: false, error: 'unsupported_sport' }, 400);
  const result = await fetchAutoNews(env, sport, { force: true });
  return json({
    ok: true,
    sport,
    refreshed: true,
    count: result.count,
    warning: result.warning || null,
  });
}

async function refreshAllSources(env) {
  const results = await Promise.all(
    SPORTS.map((sport) => fetchAutoNews(env, sport, { force: true })),
  );

  return Object.fromEntries(SPORTS.map((sport, index) => [sport, results[index]]));
}

async function handleCronRefresh(env, url) {
  const secret = url.searchParams.get('secret');
  if (!env.TICKER_CRON_SECRET || !secret || secret !== env.TICKER_CRON_SECRET) {
    return json({ ok: false, error: 'unauthorized' }, 401);
  }

  const results = await refreshAllSources(env);
  return json({
    ok: true,
    source: 'cron',
    footballCount: results.football.count,
    tennisCount: results.tennis.count,
    updatedAt: new Date().toISOString(),
  });
}

async function routeRequest(request, env) {
  const url = new URL(request.url);
  const path = url.pathname;

  if (request.method === 'OPTIONS') {
    return new Response(null, {
      status: 204,
      headers: {
        'access-control-allow-origin': '*',
        'access-control-allow-methods': 'GET,POST,DELETE,OPTIONS',
        'access-control-allow-headers': `content-type,authorization,${ADMIN_SECRET_HEADER},${ADMIN_SECRET_HEADER_ALIAS}`,
      },
    });
  }

  if (path === '/api/health') {
    return json({
      ok: true,
      service: 'ticker-hub',
      buildVersion: BUILD_VERSION,
      sports: SPORTS,
      kvConfigured: Boolean(env.TICKER_KV),
      telegramConfigured: Boolean(env.TELEGRAM_BOT_TOKEN),
    });
  }

  if (path === '/ticker/football.html' || path === '/football.html') {
    return handleTicker(request, env, 'football');
  }

  if (path === '/ticker/tennis.html' || path === '/tennis.html') {
    return handleTicker(request, env, 'tennis');
  }

  if (path === '/api/news/football-world-cup') {
    return json(await newsResponse(env, 'football', url));
  }

  if (path === '/api/news/tennis') {
    return json(await newsResponse(env, 'tennis', url));
  }

  if (path === '/api/ticker/state' && ['GET', 'POST'].includes(request.method)) {
    return handleState(request, env, url);
  }

  if (path === '/api/ticker/manual' && ['POST', 'DELETE'].includes(request.method)) {
    return handleManual(request, env, url);
  }

  if (path === '/api/ticker/clear' && request.method === 'POST') {
    return handleClear(request, env, url);
  }

  if (path === '/api/ticker/refresh' && request.method === 'POST') {
    return handleRefresh(request, env, url);
  }

  if ((path === '/api/cron/refresh' || path === '/api/cron/refresh/')
    && ['GET', 'HEAD'].includes(request.method)) {
    const response = await handleCronRefresh(env, url);
    return request.method === 'HEAD'
      ? new Response(null, { status: response.status, headers: response.headers })
      : response;
  }

  if (path === '/api/admin/env-check' && request.method === 'GET') {
    const denied = requireAdmin(request, env);
    if (denied) return denied;
    return json({
      ok: true,
      hasTickerAdminSecret: Boolean(env.TICKER_ADMIN_SECRET),
      hasTickerCronSecret: Boolean(env.TICKER_CRON_SECRET),
      hasTelegramBotToken: Boolean(env.TELEGRAM_BOT_TOKEN),
      hasTelegramWebhookSecret: Boolean(env.TELEGRAM_WEBHOOK_SECRET),
      hasKv: Boolean(env.TICKER_KV),
    });
  }

  if (path === '/api/telegram/setup' && request.method === 'POST') {
    const denied = requireAdmin(request, env);
    if (denied) return denied;
    return json(await setupTelegramWebhook(env, url.origin));
  }

  if (path === '/api/telegram/status' && request.method === 'GET') {
    const denied = requireAdmin(request, env);
    if (denied) return denied;
    return json(await getTelegramStatus(env));
  }

  if (path === '/telegram/webhook' && request.method === 'POST') {
    const result = await handleTelegramWebhook(request, env);
    return json({ ok: result.ok, configured: result.configured, error: result.error }, result.status);
  }

  if (request.method === 'GET' && env.ASSETS?.fetch) {
    return env.ASSETS.fetch(request);
  }

  return json({ ok: false, error: 'not_found', path }, 404);
}

export default {
  async fetch(request, env) {
    try {
      return await routeRequest(request, env);
    } catch (error) {
      return json({
        ok: false,
        error: error?.message || String(error),
      }, 500);
    }
  },

  async scheduled(_event, env, ctx) {
    ctx.waitUntil(refreshAllSources(env));
  },
};
