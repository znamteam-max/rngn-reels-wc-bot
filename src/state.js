import { DEFAULT_STATE, sportConfig } from './config.js';
import { getJson, setJson } from './storage.js';

const MODES = new Set(['auto', 'manual', 'mixed']);

function clampNumber(value, fallback, min, max) {
  const number = Number(value);
  if (!Number.isFinite(number)) return fallback;
  return Math.min(Math.max(Math.round(number), min), max);
}

export function normalizeState(sport, input = {}) {
  const defaults = DEFAULT_STATE[sport];
  const config = sportConfig(sport);
  if (!defaults || !config) return null;

  return {
    sport,
    enabled: input.enabled === undefined ? defaults.enabled : Boolean(input.enabled),
    mode: MODES.has(input.mode) ? input.mode : defaults.mode,
    speed: clampNumber(input.speed, defaults.speed, 12, 220),
    refreshSeconds: clampNumber(input.refreshSeconds, defaults.refreshSeconds, 15, 3600),
    limit: clampNumber(input.limit, defaults.limit, 1, config.limit),
    updatedAt: input.updatedAt || null,
  };
}

export async function getState(env, sport) {
  const config = sportConfig(sport);
  if (!config) return null;
  const stored = await getJson(env, config.stateKey, {});
  const defaultSpeed = sport === 'football'
    ? env.DEFAULT_FOOTBALL_TICKER_SPEED
    : env.DEFAULT_TENNIS_TICKER_SPEED;
  return normalizeState(sport, {
    ...(defaultSpeed ? { speed: defaultSpeed } : {}),
    ...stored,
  });
}

export async function updateState(env, sport, patch) {
  const config = sportConfig(sport);
  if (!config) return null;

  const current = await getState(env, sport);
  const next = normalizeState(sport, {
    ...current,
    ...patch,
    updatedAt: new Date().toISOString(),
  });

  return setJson(env, config.stateKey, next);
}
