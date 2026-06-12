export const BUILD_VERSION = 'ticker-hub-cron-v2-2026-06-12';
export const ADMIN_SECRET_HEADER = 'x-ticker-admin-secret';
export const ADMIN_SECRET_HEADER_ALIAS = 'x-admin-secret';
export const SPORTS = ['football', 'tennis'];

export const SOURCE_CONFIG = {
  football: {
    sport: 'football',
    competition: 'world-cup',
    sourceUrl: 'https://www.sports.ru/football/tournament/fifa-world-cup/news/',
    cacheKey: 'ticker:football:cache',
    manualKey: 'ticker:football:manual',
    stateKey: 'ticker:football:state',
    limit: 20,
    fallbackTitle: 'ЧМ-2026: футбольная бегущая строка готова к новостям',
  },
  tennis: {
    sport: 'tennis',
    competition: 'top-news',
    sourceUrl: 'https://www.sports.ru/tennis/news/top/',
    cacheKey: 'ticker:tennis:cache',
    manualKey: 'ticker:tennis:manual',
    stateKey: 'ticker:tennis:state',
    limit: 15,
    fallbackTitle: 'Новости тенниса временно недоступны',
  },
};

export const DEFAULT_STATE = {
  football: {
    sport: 'football',
    enabled: true,
    mode: 'mixed',
    speed: 70,
    refreshSeconds: 120,
    limit: 20,
  },
  tennis: {
    sport: 'tennis',
    enabled: true,
    mode: 'auto',
    speed: 100,
    refreshSeconds: 120,
    limit: 15,
  },
};

export function sportConfig(sport) {
  return SOURCE_CONFIG[sport] || null;
}

export function isSport(value) {
  return SPORTS.includes(value);
}
