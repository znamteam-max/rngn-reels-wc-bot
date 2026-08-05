import { createHash, randomUUID, timingSafeEqual } from "node:crypto";

const MAX_WORKER_CALLS = 6;
const MAX_DRAIN_MS = 50_000;
const WORKER_TIMEOUT_MS = 25_000;
const COMPLETION_RESERVE_MS = 3_000;
const MAX_RETRIES = 2;
const MAX_CHAIN_DEPTH = 2;
const MAX_BODY_BYTES = 4_096;
const WORKER_USER_AGENT = "rngn-event-kick/1.0";

let activeDrain = null;

function jsonResponse(payload, status, extraHeaders = {}) {
  return Response.json(payload, {
    status,
    headers: { "Cache-Control": "no-store", ...extraHeaders },
  });
}

function bearerToken(request) {
  const authorization = request.headers.get("authorization") || "";
  const match = authorization.match(/^Bearer\s+(.+)$/i);
  return match ? match[1] : "";
}

function constantTimeEqual(left, right) {
  const leftHash = createHash("sha256").update(left || "", "utf8").digest();
  const rightHash = createHash("sha256").update(right || "", "utf8").digest();
  return timingSafeEqual(leftHash, rightHash);
}

function safeCount(value) {
  return Number.isInteger(value) && value >= 0 ? value : null;
}

function parseWorkerPayload(payload) {
  if (!payload || payload.ok !== true) {
    return null;
  }
  const fields = [
    "claimed",
    "done",
    "retried",
    "dead",
    "remaining_ready",
    "remaining_queued_total",
  ];
  const parsed = {};
  for (const field of fields) {
    const value = safeCount(payload[field]);
    if (value === null) {
      return null;
    }
    parsed[field] = value;
  }
  const next = payload.next_available_in_seconds;
  if (next !== null && (!Number.isFinite(next) || next < 0)) {
    return null;
  }
  parsed.next_available_in_seconds = next === null ? null : Math.ceil(next);
  return parsed;
}

function sleep(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

async function fetchWithTimeout(fetchImpl, url, init, timeoutMs) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), Math.max(1, timeoutMs));
  try {
    return await fetchImpl(url, { ...init, signal: controller.signal });
  } finally {
    clearTimeout(timeout);
  }
}

function remainingMs(state) {
  return Math.max(0, state.budgetMs - (state.now() - state.startedAt));
}

function canCallWorker(state) {
  return state.calls < state.maxCalls && remainingMs(state) > COMPLETION_RESERVE_MS;
}

async function waitWithinBudget(state, milliseconds) {
  const allowed = Math.min(milliseconds, Math.max(0, remainingMs(state) - COMPLETION_RESERVE_MS));
  if (allowed <= 0) {
    return false;
  }
  await state.sleep(allowed);
  return true;
}

async function callWorker(state) {
  let retries = 0;
  while (canCallWorker(state)) {
    state.calls += 1;
    const timeoutMs = Math.min(
      WORKER_TIMEOUT_MS,
      Math.max(1, remainingMs(state) - COMPLETION_RESERVE_MS),
    );
    let response;
    try {
      response = await fetchWithTimeout(
        state.fetchImpl,
        `${state.origin}/api/cron/process-jobs`,
        {
          method: "POST",
          headers: {
            Authorization: `Bearer ${state.secret}`,
            "Content-Type": "application/json",
            "User-Agent": WORKER_USER_AGENT,
            "X-Worker-Source": "event_kick",
            "X-Worker-Drain-Id": state.drainId,
          },
          body: "{}",
        },
        timeoutMs,
      );
    } catch (error) {
      return { ok: false, reason: error?.name === "AbortError" ? "timeout" : "network" };
    }
    if (response.status === 401 || response.status === 403) {
      return { ok: false, reason: "authorization", status: response.status };
    }
    if (response.status === 429 || response.status >= 500) {
      if (retries >= MAX_RETRIES || !canCallWorker(state)) {
        return { ok: false, reason: "retry_exhausted", status: response.status };
      }
      retries += 1;
      if (!(await waitWithinBudget(state, Math.min(2_000, retries * 500)))) {
        return { ok: false, reason: "budget" };
      }
      continue;
    }
    if (!response.ok) {
      return { ok: false, reason: "http", status: response.status };
    }
    let payload;
    try {
      payload = parseWorkerPayload(await response.json());
    } catch {
      payload = null;
    }
    if (!payload) {
      return { ok: false, reason: "invalid_json" };
    }
    return { ok: true, payload };
  }
  return { ok: false, reason: "budget" };
}

async function runBatches(state) {
  while (canCallWorker(state)) {
    const result = await callWorker(state);
    if (!result.ok) {
      state.failure = result;
      return;
    }
    const batch = result.payload;
    state.claimed += batch.claimed;
    state.done += batch.done;
    state.retried += batch.retried;
    state.dead += batch.dead;
    state.remainingReady = batch.remaining_ready;
    state.remainingQueuedTotal = batch.remaining_queued_total;
    state.nextAvailableInSeconds = batch.next_available_in_seconds;
    state.processedAny ||= batch.claimed > 0 || batch.done > 0;
    console.info("event_worker_kick_batch", {
      calls: state.calls,
      claimed: batch.claimed,
      done: batch.done,
      retried: batch.retried,
      dead: batch.dead,
      remaining_ready: batch.remaining_ready,
      remaining_queued_total: batch.remaining_queued_total,
    });

    if (batch.remaining_ready > 0) {
      if (!(await waitWithinBudget(state, 1_000))) {
        return;
      }
      continue;
    }
    if (state.processedAny && !state.settled) {
      state.settled = true;
      if (!(await waitWithinBudget(state, 1_000))) {
        return;
      }
      continue;
    }
    if (batch.next_available_in_seconds !== null) {
      const waitMs = Math.max(1_000, batch.next_available_in_seconds * 1_000);
      if (waitMs <= remainingMs(state) - COMPLETION_RESERVE_MS) {
        if (!(await waitWithinBudget(state, waitMs))) {
          return;
        }
        continue;
      }
    }
    return;
  }
}

async function completeKick(state) {
  const timeoutMs = Math.min(2_000, Math.max(1, remainingMs(state)));
  try {
    const response = await fetchWithTimeout(
      state.fetchImpl,
      `${state.origin}/api/internal/complete-worker-kick`,
      {
        method: "POST",
        headers: {
          Authorization: `Bearer ${state.secret}`,
          "Content-Type": "application/json",
          "User-Agent": WORKER_USER_AGENT,
          "X-Worker-Drain-Id": state.drainId,
        },
        body: "{}",
      },
      timeoutMs,
    );
    if (!response.ok) {
      return { ok: false, ready_jobs: 0 };
    }
    const payload = await response.json();
    const readyJobs = safeCount(payload?.ready_jobs);
    return payload?.ok === true && readyJobs !== null
      ? { ok: true, ready_jobs: readyJobs }
      : { ok: false, ready_jobs: 0 };
  } catch {
    return { ok: false, ready_jobs: 0 };
  }
}

async function chainKick(state) {
  if (state.chainDepth >= MAX_CHAIN_DEPTH || remainingMs(state) <= 0) {
    return false;
  }
  try {
    const response = await fetchWithTimeout(
      state.fetchImpl,
      `${state.origin}/api/internal/kick-worker`,
      {
        method: "POST",
        headers: {
          Authorization: `Bearer ${state.secret}`,
          "User-Agent": WORKER_USER_AGENT,
          "X-Worker-Chain-Depth": String(state.chainDepth + 1),
          "Content-Length": "0",
        },
        body: "",
      },
      Math.min(1_000, Math.max(1, remainingMs(state))),
    );
    return response.status === 202;
  } catch {
    return false;
  }
}

export async function drainWorker({
  origin,
  secret,
  chainDepth = 0,
  fetchImpl = globalThis.fetch,
  sleepImpl = sleep,
  now = Date.now,
  maxCalls = MAX_WORKER_CALLS,
  budgetMs = MAX_DRAIN_MS,
  drainId = randomUUID(),
} = {}) {
  const state = {
    origin,
    secret,
    chainDepth,
    fetchImpl,
    sleep: sleepImpl,
    now,
    maxCalls: Math.min(MAX_WORKER_CALLS, Math.max(1, maxCalls)),
    budgetMs: Math.min(MAX_DRAIN_MS, Math.max(COMPLETION_RESERVE_MS + 1, budgetMs)),
    drainId,
    startedAt: now(),
    calls: 0,
    claimed: 0,
    done: 0,
    retried: 0,
    dead: 0,
    remainingReady: 0,
    remainingQueuedTotal: 0,
    nextAvailableInSeconds: null,
    processedAny: false,
    settled: false,
    failure: null,
  };
  console.info("event_worker_kick_started", { chain_depth: chainDepth });
  let completion = { ok: false, ready_jobs: 0 };
  try {
    await runBatches(state);
  } catch (error) {
    state.failure = { reason: error?.name || "unexpected" };
  } finally {
    completion = await completeKick(state);
    if (!completion.ok && !state.failure) {
      state.failure = { reason: "completion" };
    }
    if (completion.ready_jobs > 0 && canCallWorker(state)) {
      await runBatches(state);
      completion = await completeKick(state);
      if (!completion.ok && !state.failure) {
        state.failure = { reason: "completion" };
      }
    }
    if (completion.ready_jobs > 0 && !canCallWorker(state)) {
      const chained = await chainKick(state);
      if (!chained && state.chainDepth < MAX_CHAIN_DEPTH && !state.failure) {
        state.failure = { reason: "chain" };
      }
    }
  }
  const summary = {
    calls: state.calls,
    claimed: state.claimed,
    done: state.done,
    retried: state.retried,
    dead: state.dead,
    remaining_ready: Math.max(state.remainingReady, completion.ready_jobs),
    remaining_queued_total: state.remainingQueuedTotal,
    duration_ms: state.now() - state.startedAt,
  };
  if (state.failure) {
    console.error("event_worker_kick_failed", {
      ...summary,
      reason: state.failure.reason,
      status: state.failure.status,
    });
  } else {
    console.info("event_worker_kick_done", summary);
  }
  return { ...summary, failure: state.failure };
}

export async function handleKickRequest(request, context) {
  if (request.method !== "POST") {
    return jsonResponse({ ok: false, error: "method not allowed" }, 405, { Allow: "POST" });
  }
  const secret = process.env.CRON_SECRET || "";
  if (!secret) {
    return jsonResponse({ ok: false, error: "CRON_SECRET not configured" }, 500);
  }
  if (!constantTimeEqual(bearerToken(request), secret)) {
    return jsonResponse({ ok: false, error: "unauthorized" }, 401);
  }
  const contentLength = Number(request.headers.get("content-length") || "0");
  if (!Number.isFinite(contentLength) || contentLength < 0 || contentLength > MAX_BODY_BYTES) {
    return jsonResponse({ ok: false, error: "payload too large" }, 413);
  }
  const body = await request.arrayBuffer();
  if (body.byteLength > MAX_BODY_BYTES) {
    return jsonResponse({ ok: false, error: "payload too large" }, 413);
  }
  if (!context || typeof context.waitUntil !== "function") {
    return jsonResponse({ ok: false, error: "background context unavailable" }, 500);
  }
  const rawDepth = Number.parseInt(request.headers.get("x-worker-chain-depth") || "0", 10);
  const chainDepth = Number.isInteger(rawDepth) ? Math.max(0, Math.min(MAX_CHAIN_DEPTH, rawDepth)) : 0;
  if (!activeDrain) {
    const origin = new URL(request.url).origin;
    const promise = drainWorker({ origin, secret, chainDepth }).finally(() => {
      if (activeDrain === promise) {
        activeDrain = null;
      }
    });
    activeDrain = promise;
    context.waitUntil(promise);
  }
  return jsonResponse({ ok: true, accepted: true }, 202);
}

export default {
  fetch: handleKickRequest,
};
