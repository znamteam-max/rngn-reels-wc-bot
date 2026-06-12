const memoryStore = new Map();

export async function getJson(env, key, fallback = null) {
  const raw = env.TICKER_KV
    ? await env.TICKER_KV.get(key)
    : memoryStore.get(key);

  if (!raw) return fallback;

  try {
    return JSON.parse(raw);
  } catch {
    return fallback;
  }
}

export async function setJson(env, key, value) {
  const raw = JSON.stringify(value);

  if (env.TICKER_KV) {
    await env.TICKER_KV.put(key, raw);
  } else {
    memoryStore.set(key, raw);
  }

  return value;
}

export async function deleteKey(env, key) {
  if (env.TICKER_KV) {
    await env.TICKER_KV.delete(key);
  } else {
    memoryStore.delete(key);
  }
}
