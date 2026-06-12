import {
  addManualNews,
  clearManualNews,
  deleteManualNews,
  fetchAutoNews,
  getManualNews,
  getTickerNews,
} from './news.js';
import { getState, updateState } from './state.js';
import { deleteKey, getJson, setJson } from './storage.js';

const MODE_LABELS = {
  auto: 'авто',
  manual: 'ручной',
  mixed: 'смешанный',
};

const TICKER_VARIANTS = {
  football: {
    sport: 'football',
    token: 'football',
    label: '⚽ Футбол ЧМ',
    path: '/ticker/football.html',
    vmixPath: '/ticker/football.html?transparent=1',
  },
  'tennis-normal': {
    sport: 'tennis',
    token: 'tennis-normal',
    label: '🎾 Теннис — большой',
    path: '/ticker/tennis.html?height=normal',
  },
  'tennis-small': {
    sport: 'tennis',
    token: 'tennis-small',
    label: '🎾 Теннис — маленький',
    path: '/ticker/tennis.html?height=small',
  },
};

function tickerVariant(token) {
  if (token === 'tennis') return TICKER_VARIANTS['tennis-normal'];
  return TICKER_VARIANTS[token] || null;
}

function tickerPathFor(variant, sport, action = 'preview') {
  const previewPath = variant?.path
    || (sport === 'football' ? '/ticker/football.html' : '/ticker/tennis.html?height=normal');
  return action === 'url' ? variant?.vmixPath || previewPath : previewPath;
}

function button(text, callbackData) {
  return { text, callback_data: callbackData };
}

function sessionKey(chatId) {
  return `telegram:sessions:${chatId}`;
}

function publicBaseUrl(env, origin) {
  const configured = String(env.PUBLIC_BASE_URL || '').replace(/\/+$/, '');
  if (configured && !configured.includes('ticker-domain.example')) return configured;
  return origin;
}

async function telegramCall(env, method, body) {
  if (!env.TELEGRAM_BOT_TOKEN) return { ok: false, skipped: 'TELEGRAM_BOT_TOKEN is not configured' };

  const response = await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/${method}`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  });

  const data = await response.json().catch(() => ({}));
  if (!response.ok || data.ok === false) {
    throw new Error(data.description || `Telegram HTTP ${response.status}`);
  }
  return data;
}

function sendMessage(env, chatId, text, replyMarkup) {
  return telegramCall(env, 'sendMessage', {
    chat_id: chatId,
    text,
    disable_web_page_preview: true,
    ...(replyMarkup ? { reply_markup: replyMarkup } : {}),
  });
}

function editMessage(env, chatId, messageId, text, replyMarkup) {
  return telegramCall(env, 'editMessageText', {
    chat_id: chatId,
    message_id: messageId,
    text,
    disable_web_page_preview: true,
    ...(replyMarkup ? { reply_markup: replyMarkup } : {}),
  });
}

function answerCallback(env, callbackQueryId, text = '') {
  return telegramCall(env, 'answerCallbackQuery', {
    callback_query_id: callbackQueryId,
    ...(text ? { text } : {}),
  });
}

async function showHome(env, chatId, messageId = null) {
  const text = 'Бегущие строки\n\nВыберите тикер:';
  const replyMarkup = {
    inline_keyboard: [
      [button(TICKER_VARIANTS.football.label, 'sport:football')],
      [button(TICKER_VARIANTS['tennis-normal'].label, 'sport:tennis-normal')],
      [button(TICKER_VARIANTS['tennis-small'].label, 'sport:tennis-small')],
    ],
  };

  return messageId
    ? editMessage(env, chatId, messageId, text, replyMarkup)
    : sendMessage(env, chatId, text, replyMarkup);
}

async function showSportMenu(env, chatId, sport, origin, messageId = null, variantToken = sport) {
  const variant = tickerVariant(variantToken) || tickerVariant(sport);
  const state = await getState(env, sport);
  const news = await getTickerNews(env, sport, state);
  const baseUrl = publicBaseUrl(env, origin);
  const label = variant?.label || sport;
  const tickerPath = tickerPathFor(variant, sport, 'url');
  const callbackToken = variant?.token || sport;
  const text = [
    `${label} ticker`,
    '',
    `Статус: ${state.enabled ? 'включена' : 'выключена'}`,
    `Режим: ${MODE_LABELS[state.mode]}`,
    `Новостей: ${news.items.length}`,
    `Скорость: ${state.speed}`,
    '',
    `${baseUrl}${tickerPath}`,
  ].join('\n');
  const replyMarkup = {
    inline_keyboard: [
      [button('Ссылка для vMix', `${callbackToken}:url`), button('Preview', `${callbackToken}:preview`)],
      [button('Добавить новость', `${callbackToken}:add`), button('Удалить новость', `${callbackToken}:manual`)],
      [button('Очистить ручные', `${callbackToken}:clear`), button('Обновить auto news', `${callbackToken}:refresh`)],
      [button(state.enabled ? 'Выключить' : 'Включить', `${callbackToken}:toggle`)],
      [button(`Режим: ${MODE_LABELS[state.mode]}`, `${callbackToken}:mode`), button(`Скорость: ${state.speed}`, `${callbackToken}:speed`)],
      [button('Назад', 'home')],
    ],
  };

  return messageId
    ? editMessage(env, chatId, messageId, text, replyMarkup)
    : sendMessage(env, chatId, text, replyMarkup);
}

async function showManualNews(env, chatId, sport, messageId, variantToken = sport) {
  const items = await getManualNews(env, sport);
  const rows = items.slice(0, 10).map((item, index) => [
    button(`${index + 1}. ${item.title.slice(0, 42)}`, `${variantToken}:delete:${item.id}`),
  ]);
  rows.push([button('Назад', `sport:${variantToken}`)]);
  const text = items.length
    ? 'Нажмите на ручную новость, чтобы удалить её.'
    : 'Ручных новостей пока нет.';
  return editMessage(env, chatId, messageId, text, { inline_keyboard: rows });
}

function nextMode(mode) {
  if (mode === 'auto') return 'manual';
  if (mode === 'manual') return 'mixed';
  return 'auto';
}

function nextSpeed(speed) {
  const speeds = [60, 70, 100, 130];
  const index = speeds.indexOf(Number(speed));
  return speeds[(index + 1) % speeds.length];
}

function isAdmin(env, chatId) {
  return !env.ADMIN_CHAT_ID || String(env.ADMIN_CHAT_ID) === String(chatId);
}

async function handleMessage(env, message, origin) {
  const chatId = message.chat?.id;
  if (!chatId) return;

  if (!isAdmin(env, chatId)) {
    await sendMessage(env, chatId, 'Доступ запрещён.');
    return;
  }

  const text = String(message.text || '').trim();
  const session = await getJson(env, sessionKey(chatId), null);

  if (session?.action === 'add' && text && !text.startsWith('/')) {
    const result = await addManualNews(env, session.sport, { title: text });
    await deleteKey(env, sessionKey(chatId));
    await sendMessage(env, chatId, `Новость добавлена:\n${result.item.title}`);
    await showSportMenu(env, chatId, session.sport, origin, null, session.variantToken || session.sport);
    return;
  }

  if (text === '/start' || text === '/menu' || !text) {
    await showHome(env, chatId);
    return;
  }

  await showHome(env, chatId);
}

async function handleCallback(env, query, origin) {
  const chatId = query.message?.chat?.id;
  const messageId = query.message?.message_id;
  const data = String(query.data || '');
  if (!chatId || !messageId) return;

  if (!isAdmin(env, chatId)) {
    await answerCallback(env, query.id, 'Доступ запрещён');
    return;
  }

  if (data === 'home') {
    await showHome(env, chatId, messageId);
    await answerCallback(env, query.id);
    return;
  }

  if (data.startsWith('sport:')) {
    const selectedToken = data.slice('sport:'.length);
    const variant = tickerVariant(selectedToken);
    if (variant) {
      await showSportMenu(env, chatId, variant.sport, origin, messageId, variant.token);
      await answerCallback(env, query.id);
      return;
    }
  }

  const [sportToken, action, itemId] = data.split(':');
  const variant = tickerVariant(sportToken);
  const sport = variant?.sport || sportToken;
  const variantToken = variant?.token || sport;

  if (!['football', 'tennis'].includes(sport)) {
    await answerCallback(env, query.id, 'Неизвестная команда');
    return;
  }

  if (action === undefined || action === 'menu') {
    await showSportMenu(env, chatId, sport, origin, messageId, variantToken);
    await answerCallback(env, query.id);
    return;
  }

  const state = await getState(env, sport);
  const baseUrl = publicBaseUrl(env, origin);

  if (action === 'url' || action === 'preview') {
    const tickerPath = tickerPathFor(variant, sport, action);
    await sendMessage(env, chatId, `${baseUrl}${tickerPath}`);
  } else if (action === 'add') {
    await setJson(env, sessionKey(chatId), {
      action: 'add',
      sport,
      variantToken,
      createdAt: new Date().toISOString(),
    });
    await sendMessage(env, chatId, 'Пришлите текст новости одним сообщением. Максимум 180 символов.');
  } else if (action === 'manual') {
    await showManualNews(env, chatId, sport, messageId, variantToken);
  } else if (action === 'delete' && itemId) {
    const deleted = await deleteManualNews(env, sport, itemId);
    await showManualNews(env, chatId, sport, messageId, variantToken);
    await answerCallback(env, query.id, deleted ? 'Удалено' : 'Новость уже удалена');
    return;
  } else if (action === 'clear') {
    await clearManualNews(env, sport);
    await showSportMenu(env, chatId, sport, origin, messageId, variantToken);
  } else if (action === 'refresh') {
    const result = await fetchAutoNews(env, sport, { force: true });
    await showSportMenu(env, chatId, sport, origin, messageId, variantToken);
    await answerCallback(env, query.id, `Обновлено: ${result.count}`);
    return;
  } else if (action === 'toggle') {
    await updateState(env, sport, { enabled: !state.enabled });
    await showSportMenu(env, chatId, sport, origin, messageId, variantToken);
  } else if (action === 'mode') {
    await updateState(env, sport, { mode: nextMode(state.mode) });
    await showSportMenu(env, chatId, sport, origin, messageId, variantToken);
  } else if (action === 'speed') {
    await updateState(env, sport, { speed: nextSpeed(state.speed) });
    await showSportMenu(env, chatId, sport, origin, messageId, variantToken);
  }

  await answerCallback(env, query.id);
}

export async function handleTelegramWebhook(request, env) {
  if (env.TELEGRAM_WEBHOOK_SECRET) {
    const received = request.headers.get('x-telegram-bot-api-secret-token');
    if (received !== env.TELEGRAM_WEBHOOK_SECRET) {
      return { ok: false, error: 'invalid_telegram_webhook_secret', status: 401 };
    }
  }

  const update = await request.json().catch(() => ({}));
  const origin = new URL(request.url).origin;

  if (update.callback_query) {
    await handleCallback(env, update.callback_query, origin);
  } else if (update.message) {
    await handleMessage(env, update.message, origin);
  }

  return { ok: true, configured: Boolean(env.TELEGRAM_BOT_TOKEN), status: 200 };
}

export async function setupTelegramWebhook(env, origin) {
  if (!env.TELEGRAM_BOT_TOKEN) throw new Error('TELEGRAM_BOT_TOKEN is not configured');
  const url = `${publicBaseUrl(env, origin)}/telegram/webhook`;
  const result = await telegramCall(env, 'setWebhook', {
    url,
    allowed_updates: ['message', 'callback_query'],
    ...(env.TELEGRAM_WEBHOOK_SECRET ? { secret_token: env.TELEGRAM_WEBHOOK_SECRET } : {}),
  });
  return { ok: true, url, telegram: result.result };
}

export async function getTelegramStatus(env) {
  if (!env.TELEGRAM_BOT_TOKEN) {
    return { ok: false, configured: false };
  }

  const [bot, webhook] = await Promise.all([
    telegramCall(env, 'getMe', {}),
    telegramCall(env, 'getWebhookInfo', {}),
  ]);

  return {
    ok: true,
    configured: true,
    botUsername: bot.result?.username || null,
    webhookUrl: webhook.result?.url || null,
    pendingUpdateCount: webhook.result?.pending_update_count || 0,
    lastErrorDate: webhook.result?.last_error_date || null,
    lastErrorMessage: webhook.result?.last_error_message || null,
  };
}
