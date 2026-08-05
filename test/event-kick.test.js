import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  drainWorker,
  handleKickRequest,
} from "../api/internal/kick-worker.js";


function workerPayload(overrides = {}) {
  return {
    ok: true,
    claimed: 0,
    done: 0,
    retried: 0,
    dead: 0,
    remaining_ready: 0,
    remaining_queued_total: 0,
    next_available_in_seconds: null,
    ...overrides,
  };
}

function jsonResponse(payload, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function routeFetch(workerResponses, { completionReady = 0 } = {}) {
  const queue = [...workerResponses];
  const calls = { worker: 0, completion: 0, chain: 0 };
  const fetchImpl = async (url, init = {}) => {
    if (url.endsWith("/api/cron/process-jobs")) {
      calls.worker += 1;
      const next = queue.shift();
      return typeof next === "function" ? next(url, init) : next;
    }
    if (url.endsWith("/api/internal/complete-worker-kick")) {
      calls.completion += 1;
      return jsonResponse({ ok: true, ready_jobs: completionReady });
    }
    if (url.endsWith("/api/internal/kick-worker")) {
      calls.chain += 1;
      return jsonResponse({ ok: true, accepted: true }, 202);
    }
    throw new Error("unexpected route");
  };
  return { fetchImpl, calls };
}

test("kicker rejects non-POST and invalid auth", async () => {
  process.env.CRON_SECRET = "test-secret";
  const context = { waitUntil() { throw new Error("must not start"); } };
  const getResponse = await handleKickRequest(
    new Request("https://example.test/api/internal/kick-worker"),
    context,
  );
  assert.equal(getResponse.status, 405);
  const unauthorized = await handleKickRequest(
    new Request("https://example.test/api/internal/kick-worker", {
      method: "POST",
      headers: { Authorization: "Bearer wrong" },
    }),
    context,
  );
  assert.equal(unauthorized.status, 401);

  const oversized = await handleKickRequest(
    new Request("https://example.test/api/internal/kick-worker", {
      method: "POST",
      headers: { Authorization: "Bearer test-secret" },
      body: "x".repeat(5_000),
    }),
    context,
  );
  assert.equal(oversized.status, 413);
});

test("valid request returns 202 before waitUntil drain finishes", async () => {
  process.env.CRON_SECRET = "test-secret";
  let releaseWorker;
  let workerFetches = 0;
  const pendingWorker = new Promise((resolve) => {
    releaseWorker = () => resolve(jsonResponse(workerPayload()));
  });
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (url) => {
    if (String(url).endsWith("/api/cron/process-jobs")) {
      workerFetches += 1;
      return pendingWorker;
    }
    return jsonResponse({ ok: true, ready_jobs: 0 });
  };
  let backgroundPromise;
  try {
    const response = await handleKickRequest(
      new Request("https://example.test/api/internal/kick-worker", {
        method: "POST",
        headers: { Authorization: "Bearer test-secret", "Content-Length": "0" },
      }),
      { waitUntil(promise) { backgroundPromise = promise; } },
    );
    assert.equal(response.status, 202);
    assert.ok(backgroundPromise instanceof Promise);
    let secondWaitUntil = false;
    const secondResponse = await handleKickRequest(
      new Request("https://example.test/api/internal/kick-worker", {
        method: "POST",
        headers: { Authorization: "Bearer test-secret", "Content-Length": "0" },
      }),
      { waitUntil() { secondWaitUntil = true; } },
    );
    assert.equal(secondResponse.status, 202);
    assert.equal(secondWaitUntil, false);
    assert.equal(workerFetches, 1);
    releaseWorker();
    await backgroundPromise;
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("drain processes multiple batches and one settle pass", async () => {
  const routed = routeFetch([
    jsonResponse(workerPayload({ claimed: 1, done: 1, remaining_ready: 1, remaining_queued_total: 1 })),
    jsonResponse(workerPayload({ claimed: 1, done: 1 })),
    jsonResponse(workerPayload()),
  ]);
  const sleeps = [];
  const result = await drainWorker({
    origin: "https://example.test",
    secret: "secret",
    fetchImpl: routed.fetchImpl,
    sleepImpl: async (milliseconds) => sleeps.push(milliseconds),
    now: () => 1_000,
  });
  assert.equal(result.calls, 3);
  assert.equal(result.done, 2);
  assert.deepEqual(sleeps, [1_000, 1_000]);
  assert.equal(routed.calls.completion, 1);
});

test("near future retry waits while far retry exits", async () => {
  const near = routeFetch([
    jsonResponse(workerPayload({ remaining_queued_total: 1, next_available_in_seconds: 1 })),
    jsonResponse(workerPayload()),
  ]);
  const nearSleeps = [];
  const nearResult = await drainWorker({
    origin: "https://example.test",
    secret: "secret",
    fetchImpl: near.fetchImpl,
    sleepImpl: async (milliseconds) => nearSleeps.push(milliseconds),
    now: () => 1_000,
  });
  assert.equal(nearResult.calls, 2);
  assert.deepEqual(nearSleeps, [1_000]);

  const far = routeFetch([
    jsonResponse(workerPayload({ remaining_queued_total: 1, next_available_in_seconds: 60 })),
  ]);
  const farResult = await drainWorker({
    origin: "https://example.test",
    secret: "secret",
    fetchImpl: far.fetchImpl,
    sleepImpl: async () => assert.fail("far retry must not wait"),
    now: () => 1_000,
  });
  assert.equal(farResult.calls, 1);
});

test("authorization and invalid JSON stop safely", async () => {
  const unauthorized = routeFetch([jsonResponse({ error: "unauthorized" }, 401)]);
  const authResult = await drainWorker({
    origin: "https://example.test",
    secret: "secret",
    fetchImpl: unauthorized.fetchImpl,
    sleepImpl: async () => {},
    now: () => 1_000,
  });
  assert.equal(authResult.calls, 1);
  assert.equal(authResult.failure.reason, "authorization");

  const invalid = routeFetch([new Response("not-json", { status: 200 })]);
  const invalidResult = await drainWorker({
    origin: "https://example.test",
    secret: "secret",
    fetchImpl: invalid.fetchImpl,
    sleepImpl: async () => {},
    now: () => 1_000,
  });
  assert.equal(invalidResult.calls, 1);
  assert.equal(invalidResult.failure.reason, "invalid_json");
});

test("retryable worker failures retry at most twice", async () => {
  const routed = routeFetch([
    jsonResponse({ error: "temporary" }, 500),
    jsonResponse({ error: "temporary" }, 503),
    jsonResponse(workerPayload()),
  ]);
  const result = await drainWorker({
    origin: "https://example.test",
    secret: "secret",
    fetchImpl: routed.fetchImpl,
    sleepImpl: async () => {},
    now: () => 1_000,
  });
  assert.equal(result.calls, 3);
  assert.equal(result.failure, null);
});

test("worker calls and chain depth are capped", async () => {
  const responses = Array.from({ length: 6 }, () =>
    jsonResponse(workerPayload({ claimed: 1, done: 1, remaining_ready: 1, remaining_queued_total: 1 })),
  );
  const capped = routeFetch(responses, { completionReady: 1 });
  const result = await drainWorker({
    origin: "https://example.test",
    secret: "secret",
    chainDepth: 2,
    fetchImpl: capped.fetchImpl,
    sleepImpl: async () => {},
    now: () => 1_000,
  });
  assert.equal(result.calls, 6);
  assert.equal(capped.calls.chain, 0);

  const chained = routeFetch([
    jsonResponse(workerPayload({ claimed: 1, done: 1, remaining_ready: 1, remaining_queued_total: 1 })),
  ], { completionReady: 1 });
  await drainWorker({
    origin: "https://example.test",
    secret: "secret",
    chainDepth: 1,
    maxCalls: 1,
    fetchImpl: chained.fetchImpl,
    sleepImpl: async () => {},
    now: () => 1_000,
  });
  assert.equal(chained.calls.chain, 1);
});

test("kicker has no arbitrary target and never logs secret", async () => {
  const source = await readFile(new URL("../api/internal/kick-worker.js", import.meta.url), "utf8");
  assert.doesNotMatch(source, /request\.(json|formData)\(/);
  assert.match(source, /\/api\/cron\/process-jobs/);

  const routed = routeFetch([jsonResponse(workerPayload())]);
  const captured = [];
  const originalInfo = console.info;
  const originalError = console.error;
  console.info = (...args) => captured.push(args);
  console.error = (...args) => captured.push(args);
  try {
    await drainWorker({
      origin: "https://example.test",
      secret: "super-secret-value",
      fetchImpl: routed.fetchImpl,
      sleepImpl: async () => {},
      now: () => 1_000,
    });
  } finally {
    console.info = originalInfo;
    console.error = originalError;
  }
  assert.doesNotMatch(JSON.stringify(captured), /super-secret-value/);
});
