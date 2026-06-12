import { sportConfig } from './config.js';
import { getJson, setJson } from './storage.js';

function decodeHtml(value) {
  return String(value || '')
    .replace(/&nbsp;|&#160;/gi, ' ')
    .replace(/&amp;/gi, '&')
    .replace(/&quot;/gi, '"')
    .replace(/&#039;|&apos;/gi, "'")
    .replace(/&lt;/gi, '<')
    .replace(/&gt;/gi, '>')
    .replace(/&#x([0-9a-f]+);/gi, (_, code) => String.fromCodePoint(parseInt(code, 16)))
    .replace(/&#(\d+);/g, (_, code) => String.fromCodePoint(Number(code)))
    .replace(/\s+/g, ' ')
    .trim();
}

function stripTags(value) {
  return decodeHtml(String(value || '').replace(/<script[\s\S]*?<\/script>/gi, ' ').replace(/<[^>]+>/g, ' '));
}

function absoluteUrl(href, base) {
  try {
    const url = new URL(href, base);
    url.hash = '';
    return url.toString();
  } catch {
    return null;
  }
}

function normalizeTitle(title) {
  return String(title || '')
    .toLowerCase()
    .replace(/https?:\/\/\S+/g, '')
    .replace(/[^\p{L}\p{N}]+/gu, ' ')
    .trim();
}

function hashString(value) {
  let hash = 0;
  const string = String(value || '');
  for (let index = 0; index < string.length; index += 1) {
    hash = ((hash << 5) - hash + string.charCodeAt(index)) | 0;
  }
  return Math.abs(hash);
}

export function dedupeItems(items) {
  const seen = new Set();
  const result = [];

  for (const item of items) {
    const titleKey = normalizeTitle(item?.title);
    const urlKey = String(item?.url || '').toLowerCase();
    if (!titleKey || seen.has(titleKey) || (urlKey && seen.has(urlKey))) continue;
    seen.add(titleKey);
    if (urlKey) seen.add(urlKey);
    result.push(item);
  }

  return result;
}

function looksLikeNewsLink(href, title, config) {
  const cleanTitle = String(title || '').trim();
  const url = absoluteUrl(href, config.sourceUrl);
  if (!url) return false;
  const path = new URL(url).pathname;
  const newsPathPattern = new RegExp(`^/${config.sport}/\\d{6,}-[^/]+\\.html$`, 'i');

  if (cleanTitle.length < 12 || cleanTitle.length > 240) return false;
  if (!newsPathPattern.test(path)) return false;
  if (/коммент|читать|подпис|реклам|ставк|прогноз|результат|турнирная таблица/i.test(cleanTitle)) return false;
  return true;
}

export function parseSportsNewsPage(sourceHtml, sport, now = new Date().toISOString()) {
  const config = sportConfig(sport);
  if (!config) return [];

  const candidates = [];
  const anchorPattern = /<a\b[^>]*href=["']([^"']+)["'][^>]*>([\s\S]*?)<\/a>/gi;
  let match;

  while ((match = anchorPattern.exec(String(sourceHtml || '')))) {
    const title = stripTags(match[2]);
    if (!looksLikeNewsLink(match[1], title, config)) continue;

    const url = absoluteUrl(match[1], config.sourceUrl);
    if (!url) continue;

    candidates.push({
      id: `sportsru-${hashString(url || title)}`,
      sport: config.sport,
      competition: config.competition,
      source: 'sports.ru',
      sourceName: 'Sports.ru',
      title,
      url,
      approved: true,
      createdAt: now,
    });
  }

  return dedupeItems(candidates).slice(0, config.limit);
}

function fallbackItems(sport) {
  const config = sportConfig(sport);
  return [{
    id: `demo-${sport}-1`,
    sport,
    competition: config.competition,
    source: 'demo',
    sourceName: 'Demo',
    title: config.fallbackTitle,
    url: config.sourceUrl,
    approved: true,
    createdAt: new Date().toISOString(),
  }];
}

export async function fetchAutoNews(env, sport, { force = false } = {}) {
  const config = sportConfig(sport);
  if (!config) throw new Error('unsupported_sport');

  const cached = await getJson(env, config.cacheKey, null);
  const now = Date.now();
  const ttlMs = Math.max(15000, Number(env.NEWS_REFRESH_SECONDS || 120) * 1000);

  if (!force && cached?.items?.length && cached.updatedAtMs && now - cached.updatedAtMs < ttlMs) {
    return { ...cached, fromCache: true };
  }

  try {
    const response = await fetch(config.sourceUrl, {
      headers: {
        'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'accept-language': 'ru,en;q=0.8',
        'user-agent': 'Mozilla/5.0 (compatible; TickerHub/1.0; +https://bolshe.media)',
      },
      cf: { cacheTtl: 30, cacheEverything: false },
    });

    if (!response.ok) throw new Error(`sports.ru HTTP ${response.status}`);

    const items = parseSportsNewsPage(await response.text(), sport);
    if (!items.length) throw new Error('no_news_items_parsed');

    const payload = {
      ok: true,
      sport,
      competition: config.competition,
      sourceUrl: config.sourceUrl,
      items,
      count: items.length,
      updatedAt: new Date().toISOString(),
      updatedAtMs: now,
      fromCache: false,
    };

    await setJson(env, config.cacheKey, payload);
    return payload;
  } catch (error) {
    if (cached?.items?.length) {
      return {
        ...cached,
        ok: true,
        fromCache: true,
        warning: `source_fetch_failed_using_cache: ${error.message}`,
      };
    }

    const items = fallbackItems(sport);
    return {
      ok: true,
      sport,
      competition: config.competition,
      sourceUrl: config.sourceUrl,
      items,
      count: items.length,
      updatedAt: new Date().toISOString(),
      updatedAtMs: now,
      fromCache: false,
      warning: `source_fetch_failed_using_demo: ${error.message}`,
    };
  }
}

export async function getManualNews(env, sport) {
  const config = sportConfig(sport);
  return config ? getJson(env, config.manualKey, []) : [];
}

export async function addManualNews(env, sport, input) {
  const config = sportConfig(sport);
  if (!config) throw new Error('unsupported_sport');

  const title = String(input.title || input.text || '').replace(/\s+/g, ' ').trim();
  if (!title) throw new Error('missing_title');

  const item = {
    id: `manual-${sport}-${Date.now()}`,
    sport,
    competition: config.competition,
    source: 'manual',
    sourceName: 'Telegram/manual',
    title: title.slice(0, 180),
    priority: Number(input.priority || 10),
    approved: input.approved !== false,
    createdAt: new Date().toISOString(),
  };

  const current = await getManualNews(env, sport);
  const next = dedupeItems([item, ...current]).slice(0, config.limit);
  await setJson(env, config.manualKey, next);
  return { item, items: next };
}

export async function deleteManualNews(env, sport, id) {
  const config = sportConfig(sport);
  if (!config) throw new Error('unsupported_sport');

  const current = await getManualNews(env, sport);
  const next = current.filter((item) => item.id !== id);
  await setJson(env, config.manualKey, next);
  return current.length !== next.length;
}

export async function clearManualNews(env, sport) {
  const config = sportConfig(sport);
  if (!config) throw new Error('unsupported_sport');
  await setJson(env, config.manualKey, []);
}

export async function getTickerNews(env, sport, state, options = {}) {
  const config = sportConfig(sport);
  if (!config) throw new Error('unsupported_sport');

  const [auto, manual] = await Promise.all([
    state.mode === 'manual' ? Promise.resolve(null) : fetchAutoNews(env, sport, options),
    state.mode === 'auto' ? Promise.resolve([]) : getManualNews(env, sport),
  ]);

  const autoItems = auto?.items || [];
  const combined = state.mode === 'manual'
    ? manual
    : state.mode === 'auto'
      ? autoItems
      : [...manual, ...autoItems];

  return {
    auto,
    manual,
    items: dedupeItems(combined)
      .filter((item) => item?.approved !== false && item?.title)
      .slice(0, state.limit),
  };
}
