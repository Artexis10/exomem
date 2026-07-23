import assert from "node:assert/strict";
import test from "node:test";

import worker, {
  ExomemState,
  evaluateReadiness,
  isMcpToolCall,
} from "../src/worker.js";

class MemoryStorage {
  constructor() {
    this.data = new Map();
  }
  async get(key) {
    if (Array.isArray(key)) return new Map(key.filter((k) => this.data.has(k)).map((k) => [k, this.data.get(k)]));
    return this.data.get(key);
  }
  async put(key, value) {
    if (typeof key === "object") for (const [k, v] of Object.entries(key)) this.data.set(k, v);
    else this.data.set(key, value);
  }
  async delete(key) {
    if (Array.isArray(key)) return key.map((k) => this.data.delete(k)).filter(Boolean).length;
    return this.data.delete(key);
  }
  async list({ prefix = "" } = {}) {
    return new Map(
      [...this.data.entries()]
        .filter(([key]) => key.startsWith(prefix))
        .sort(([left], [right]) => left.localeCompare(right)),
    );
  }
  async transaction(fn) {
    return fn(this);
  }
}

const body = async (response) => response.json();
const post = (path, value) =>
  new Request(`https://state${path}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(value),
  });

test("lease grants one replica, rejects the other, and increments fencing on takeover", async () => {
  const object = new ExomemState({ storage: new MemoryStorage() });
  const desktop = await body(await object.fetch(post("/lease?operation=acquire", { replica_id: "desktop", ttl_seconds: 30 })));
  assert.equal(desktop.granted, true);
  assert.equal(desktop.fencing_token, 1);

  const blocked = await body(await object.fetch(post("/lease?operation=acquire", { replica_id: "laptop", ttl_seconds: 30 })));
  assert.equal(blocked.granted, false);
  assert.equal(blocked.holder, "desktop");

  const released = await body(await object.fetch(post("/lease?operation=release", { replica_id: "desktop", fencing_token: 1 })));
  assert.equal(released.granted, true);
  const laptop = await body(await object.fetch(post("/lease?operation=acquire", { replica_id: "laptop", ttl_seconds: 30 })));
  assert.equal(laptop.granted, true);
  assert.equal(laptop.fencing_token, 2);

  const stale = await body(await object.fetch(post("/lease?operation=renew", { replica_id: "desktop", fencing_token: 1, ttl_seconds: 30 })));
  assert.equal(stale.granted, false);
  assert.equal(stale.holder, "laptop");
});

test("opaque shared state supports TTL and bulk operations", async () => {
  const object = new ExomemState({ storage: new MemoryStorage() });
  await object.fetch(post("/state/put", { collection: "tokens", key: "a", value: { ciphertext: "one" }, ttl: 60 }));
  const value = await body(await object.fetch(post("/state/get", { collection: "tokens", key: "a" })));
  assert.deepEqual(value.result, { ciphertext: "one" });

  await object.fetch(post("/state/put-many", {
    collection: "tokens",
    keys: ["b", "c"],
    values: [{ ciphertext: "two" }, { ciphertext: "three" }],
  }));
  const values = await body(await object.fetch(post("/state/get-many", { collection: "tokens", keys: ["a", "b", "missing"] })));
  assert.deepEqual(values.result, [{ ciphertext: "one" }, { ciphertext: "two" }, null]);
});

test("shared state rejects a JSON null body with the documented response", async () => {
  const object = new ExomemState({ storage: new MemoryStorage() });

  const response = await object.fetch(post("/state/list-keys", null));

  assert.equal(response.status, 400);
  assert.deepEqual(await body(response), { error: "invalid request" });
});

test("atomic state creation never replaces a live existing value", async () => {
  const object = new ExomemState({ storage: new MemoryStorage() });
  const first = await object.fetch(post("/state/put-if-absent", {
    collection: "auth",
    key: "generation",
    value: { __encrypted_data__: "first-ciphertext" },
    ttl: null,
  }));
  const second = await object.fetch(post("/state/put-if-absent", {
    collection: "auth",
    key: "generation",
    value: { __encrypted_data__: "replacement-ciphertext" },
    ttl: null,
  }));
  const stored = await body(await object.fetch(post("/state/get", {
    collection: "auth",
    key: "generation",
  })));

  assert.equal(first.status, 200);
  assert.deepEqual(await body(first), { result: true });
  assert.equal(second.status, 200);
  assert.deepEqual(await body(second), { result: false });
  assert.deepEqual(stored.result, { __encrypted_data__: "first-ciphertext" });
});

test("atomic state creation replaces a TTL-expired value", async () => {
  const object = new ExomemState({ storage: new MemoryStorage() });
  const originalNow = Date.now;
  let now = 100_000;
  Date.now = () => now;
  try {
    const first = await body(await object.fetch(post("/state/put-if-absent", {
      collection: "auth",
      key: "generation",
      value: { __encrypted_data__: "expired-ciphertext" },
      ttl: 1,
    })));
    now += 2_000;
    const replacement = await body(await object.fetch(post("/state/put-if-absent", {
      collection: "auth",
      key: "generation",
      value: { __encrypted_data__: "current-ciphertext" },
      ttl: null,
    })));
    const stored = await body(await object.fetch(post("/state/get", {
      collection: "auth",
      key: "generation",
    })));

    assert.deepEqual(first, { result: true });
    assert.deepEqual(replacement, { result: true });
    assert.deepEqual(stored.result, { __encrypted_data__: "current-ciphertext" });
  } finally {
    Date.now = originalNow;
  }
});

test("state key enumeration returns sorted live keys without encrypted values", async () => {
  const object = new ExomemState({ storage: new MemoryStorage() });
  const originalNow = Date.now;
  let now = 100_000;
  Date.now = () => now;
  try {
    await object.fetch(post("/state/put", {
      collection: "auth",
      key: "permanent",
      value: { __encrypted_data__: "permanent-secret-ciphertext" },
    }));
    await object.fetch(post("/state/put", {
      collection: "auth",
      key: "expired",
      value: { __encrypted_data__: "expired-secret-ciphertext" },
      ttl: 1,
    }));
    await object.fetch(post("/state/put", {
      collection: "other",
      key: "hidden",
      value: { __encrypted_data__: "other-secret-ciphertext" },
    }));
    await object.fetch(post("/state/put", {
      collection: "auth",
      key: "alpha",
      value: { __encrypted_data__: "alpha-secret-ciphertext" },
    }));
    now += 2_000;

    const response = await object.fetch(post("/state/list-keys", { collection: "auth" }));
    const rendered = await response.text();

    assert.equal(response.status, 200);
    assert.deepEqual(JSON.parse(rendered), { result: ["alpha", "permanent"] });
    assert.equal(rendered.includes("ciphertext"), false);
    assert.equal(rendered.includes("secret"), false);
  } finally {
    Date.now = originalNow;
  }
});

test("edge rejects unauthenticated lease and state coordinator access", async () => {
  const env = { STATE_TOKEN: "secret", EXOMEM_STATE: { idFromName: (name) => name, get: () => { throw new Error("must not reach state"); } } };
  const requests = [
    post("/v1/vaults/main/lease/acquire", { replica_id: "desktop", ttl_seconds: 30 }),
    post("/v1/state/main/put-if-absent", {
      collection: "auth",
      key: "generation",
      value: { __encrypted_data__: "ciphertext" },
    }),
    post("/v1/state/main/list-keys", { collection: "auth" }),
  ];
  for (const request of requests) {
    const response = await worker.fetch(request, env);
    assert.equal(response.status, 401);
    assert.deepEqual(await body(response), { error: "unauthorized" });
  }
});

test("edge accepts a piped Worker secret with trailing transport whitespace", async () => {
  const request = post("/v1/vaults/main/lease/acquire", {
    replica_id: "desktop",
    ttl_seconds: 30,
  });
  request.headers.set("authorization", "Bearer secret");
  const env = {
    STATE_TOKEN: "secret\r\n",
    EXOMEM_STATE: {
      idFromName: (name) => name,
      get: () => ({ fetch: async () => new Response('{"granted":true}') }),
    },
  };
  const response = await worker.fetch(request, env);
  assert.equal(response.status, 200);
  assert.equal((await response.json()).granted, true);
});

const mcp = (payload) =>
  new Request("https://exomem.example.com/mcp", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: typeof payload === "string" ? payload : JSON.stringify(payload),
  });

const readiness = (replicaId, overrides = {}) => ({
  status: "ready",
  service: "exomem",
  release: "0.20.2",
  runtime_contract: 1,
  transport: "streamable-http-stateless",
  replica_id: replicaId,
  coordination: { enabled: true, role: "follower", coordinator_healthy: true },
  takeover_eligible: true,
  reasons: [],
  ...overrides,
});

const admission = (replicaId, fencingToken = 7) => ({
  holder: replicaId,
  fencing_token: fencingToken,
  readiness: readiness(replicaId),
});

const leaseRecord = (value) => {
  if (value && typeof value === "object") return value;
  if (value == null) return { holder: null, expires_at: null, fencing_token: 7 };
  return {
    holder: value,
    expires_at: Date.now() / 1000 + 30,
    fencing_token: 7,
    admission: admission(value),
  };
};

const edgeEnv = (lease = "desktop", stateFetch = null) => ({
  VAULT_ID: "personal-main",
  DESKTOP_REPLICA_ID: "desktop",
  LAPTOP_REPLICA_ID: "laptop",
  DESKTOP_ORIGIN: "https://desktop.example.com",
  LAPTOP_ORIGIN: "https://laptop.example.com",
  ORIGIN_TIMEOUT_MS: "2500",
  MCP_TOOL_TIMEOUT_MS: "15000",
  SUPPORTED_RUNTIME_CONTRACTS: "1",
  REQUIRE_COORDINATION: "true",
  EXOMEM_STATE: {
    idFromName: (name) => name,
    get: () => ({
      fetch: stateFetch || (async () => new Response(JSON.stringify(leaseRecord(lease)), {
        headers: { "content-type": "application/json" },
      })),
    }),
  },
});

const toolCall = (name = "remember") => mcp({
  jsonrpc: "2.0",
  id: 1,
  method: "tools/call",
  params: { name, arguments: {} },
});

const restMutation = (name = "remember") =>
  new Request(`https://exomem.example.com/api/${name}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ content: "one canonical payload" }),
  });

const transferUpload = () =>
  new Request("https://exomem.example.com/public/exomem/v2/transfers/upload", {
    method: "PUT",
    headers: { "content-type": "application/octet-stream" },
    body: "artifact bytes",
  });

test("classifies single and batched tool calls conservatively", async () => {
  assert.equal(await isMcpToolCall(toolCall()), true);
  assert.equal(await isMcpToolCall(mcp([
    { jsonrpc: "2.0", method: "notifications/initialized" },
    { jsonrpc: "2.0", id: 2, method: "tools/call", params: { name: "ask_memory", arguments: {} } },
  ])), true);
  assert.equal(await isMcpToolCall(mcp({ jsonrpc: "2.0", id: 1, method: "initialize" })), false);
  assert.equal(await isMcpToolCall(mcp("{not-json")), true);
  assert.equal(await isMcpToolCall(new Request("https://exomem.example.com/mcp")), false);
});

test("readiness admission uses compatibility rather than exact release equality", () => {
  const env = edgeEnv();
  assert.deepEqual(evaluateReadiness(readiness("desktop", { release: "0.20.99" }), env, "desktop"), {
    eligible: true,
    reason: null,
  });
  assert.equal(
    evaluateReadiness(readiness("desktop", { runtime_contract: 2 }), env, "desktop").reason,
    "unsupported_runtime_contract",
  );
  assert.equal(
    evaluateReadiness(readiness("desktop", { transport: "streamable-http-stateful" }), env, "desktop").reason,
    "unsupported_transport",
  );
  assert.equal(
    evaluateReadiness(readiness("laptop"), env, "desktop").reason,
    "replica_identity_mismatch",
  );
  assert.equal(
    evaluateReadiness(readiness("desktop", {
      coordination: { enabled: false, role: "standalone", coordinator_healthy: true },
    }), env, "desktop").reason,
    "coordination_required",
  );
});

test("lease admission is bound to holder and fencing token and cleared on takeover", async () => {
  const object = new ExomemState({ storage: new MemoryStorage() });
  const desktop = await body(await object.fetch(post("/lease?operation=acquire", {
    replica_id: "desktop",
    ttl_seconds: 30,
  })));
  const admitted = await object.fetch(post("/admission", {
    holder: "desktop",
    fencing_token: desktop.fencing_token,
    readiness: readiness("desktop"),
  }));
  assert.equal(admitted.status, 200);
  let status = await body(await object.fetch(new Request("https://state/lease")));
  assert.equal(status.admission.fencing_token, desktop.fencing_token);

  await object.fetch(post("/lease?operation=release", {
    replica_id: "desktop",
    fencing_token: desktop.fencing_token,
  }));
  const laptop = await body(await object.fetch(post("/lease?operation=acquire", {
    replica_id: "laptop",
    ttl_seconds: 30,
  })));
  status = await body(await object.fetch(new Request("https://state/lease")));
  assert.equal(status.holder, "laptop");
  assert.equal(status.admission, undefined);

  const stale = await object.fetch(post("/admission", {
    holder: "desktop",
    fencing_token: desktop.fencing_token,
    readiness: readiness("desktop"),
  }));
  assert.equal(stale.status, 409);
  assert.equal(laptop.fencing_token, 2);
});

test("active-holder tool call uses the long timeout and is never replayed", async () => {
  const calls = [];
  const requestIds = [];
  const timeouts = [];
  const originalFetch = globalThis.fetch;
  const originalTimeout = AbortSignal.timeout;
  globalThis.fetch = async (request) => {
    calls.push(new URL(request.url).origin);
    requestIds.push(request.headers.get("x-exomem-request-id"));
    return new Response("tool failed after execution", { status: 500 });
  };
  AbortSignal.timeout = (ms) => {
    timeouts.push(ms);
    return originalTimeout(60_000);
  };
  try {
    const response = await worker.fetch(toolCall(), edgeEnv("desktop"));
    assert.equal(response.status, 500);
    assert.deepEqual(calls, ["https://desktop.example.com"]);
    assert.match(requestIds[0], /^[0-9a-f-]{36}$/);
    assert.deepEqual(timeouts, [15_000]);
  } finally {
    globalThis.fetch = originalFetch;
    AbortSignal.timeout = originalTimeout;
  }
});

test("edge replaces non-UUID mutation correlation text before forwarding and logging", async () => {
  const forwarded = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (request) => {
    forwarded.push(request.headers.get("x-exomem-request-id"));
    return new Response("ok", { status: 200 });
  };
  try {
    const request = toolCall();
    request.headers.set("x-exomem-request-id", "attacker-controlled-log-text");
    const response = await worker.fetch(request, edgeEnv("desktop"));
    assert.equal(response.status, 200);
    assert.equal(forwarded.length, 1);
    assert.match(
      forwarded[0],
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
    );
    assert.notEqual(forwarded[0], "attacker-controlled-log-text");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("active-holder tool timeout fails closed without passive replay", async () => {
  const calls = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (request) => {
    calls.push(new URL(request.url).origin);
    throw new DOMException("timed out", "TimeoutError");
  };
  try {
    const response = await worker.fetch(toolCall(), edgeEnv("desktop"));
    assert.equal(response.status, 504);
    assert.deepEqual(calls, ["https://desktop.example.com"]);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("personal REST mutation failure and timeout never replay to passive origin", async () => {
  for (const outcome of ["response", "timeout"]) {
    const calls = [];
    const originalFetch = globalThis.fetch;
    globalThis.fetch = async (request) => {
      calls.push(new URL(request.url).origin);
      if (outcome === "timeout") throw new DOMException("timed out", "TimeoutError");
      return new Response("write failed after possible commit", { status: 500 });
    };
    try {
      const response = await worker.fetch(restMutation(), edgeEnv("desktop"));
      assert.equal(response.status, outcome === "timeout" ? 504 : 500);
      assert.deepEqual(calls, ["https://desktop.example.com"]);
    } finally {
      globalThis.fetch = originalFetch;
    }
  }
});

test("public transfer PUT failure never replays to passive origin", async () => {
  const calls = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (request) => {
    calls.push(new URL(request.url).origin);
    return new Response("upload failed after possible commit", { status: 500 });
  };
  try {
    const response = await worker.fetch(transferUpload(), edgeEnv("desktop"));
    assert.equal(response.status, 500);
    assert.deepEqual(calls, ["https://desktop.example.com"]);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("active holder without cached admission is probed once and recorded", async () => {
  const calls = [];
  const stateCalls = [];
  const lease = {
    holder: "desktop",
    expires_at: Date.now() / 1000 + 30,
    fencing_token: 12,
  };
  const stateFetch = async (request) => {
    const url = new URL(request.url);
    stateCalls.push({ path: url.pathname, method: request.method });
    if (url.pathname === "/admission") return new Response('{"stored":true}', { status: 200 });
    return new Response(JSON.stringify(lease), {
      headers: { "content-type": "application/json" },
    });
  };
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (request) => {
    const url = new URL(request.url);
    calls.push(url.pathname);
    if (url.pathname === "/health/ready") {
      return Response.json(readiness("desktop"));
    }
    return new Response("ok", { status: 200 });
  };
  try {
    const response = await worker.fetch(toolCall(), edgeEnv(lease, stateFetch));
    assert.equal(response.status, 200);
    assert.deepEqual(calls, ["/health/ready", "/mcp"]);
    assert.deepEqual(stateCalls, [
      { path: "/lease", method: "GET" },
      { path: "/admission", method: "POST" },
    ]);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("ineligible active holder fails closed without invoking either MCP origin", async () => {
  const calls = [];
  const lease = {
    holder: "desktop",
    expires_at: Date.now() / 1000 + 30,
    fencing_token: 12,
  };
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (request) => {
    calls.push(new URL(request.url).pathname);
    return Response.json(readiness("desktop", { runtime_contract: 99 }));
  };
  try {
    const response = await worker.fetch(toolCall(), edgeEnv(lease));
    assert.equal(response.status, 503);
    assert.deepEqual(calls, ["/health/ready"]);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("no-holder tool call probes and selects exactly one healthy origin", async () => {
  const calls = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (request) => {
    const url = new URL(request.url);
    calls.push(url.href);
    if (url.pathname === "/health/ready") {
      return Response.json(
        url.origin.includes("laptop")
          ? readiness("laptop")
          : readiness("desktop", { runtime_contract: 99 }),
      );
    }
    return new Response("laptop tool result", { status: 200 });
  };
  try {
    const response = await worker.fetch(toolCall(), edgeEnv(null));
    assert.equal(response.status, 200);
    assert.equal(await response.text(), "laptop tool result");
    const forwarded = calls.filter((url) => new URL(url).pathname === "/mcp");
    assert.deepEqual(forwarded, ["https://laptop.example.com/mcp"]);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("no-holder tool call is not forwarded when neither origin is healthy", async () => {
  const calls = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (request) => {
    calls.push(new URL(request.url).pathname);
    return new Response("down", { status: 503 });
  };
  try {
    const response = await worker.fetch(toolCall(), edgeEnv(null));
    assert.equal(response.status, 503);
    assert.deepEqual(calls, [
      "/health/ready",
      "/health/ready",
    ]);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("cached active-holder admission preserves the steady-state single-fetch path", async () => {
  const calls = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (request) => {
    calls.push(new URL(request.url).pathname);
    return new Response("cached writer", { status: 200 });
  };
  try {
    const response = await worker.fetch(toolCall(), edgeEnv("desktop"));
    assert.equal(response.status, 200);
    assert.equal(await response.text(), "cached writer");
    assert.deepEqual(calls, ["/mcp"]);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

async function expectedEdgeAuth(token, requestId) {
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(token),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const signature = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(requestId));
  return [...new Uint8Array(signature)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

// Golden cross-implementation vector: tests/test_edge_ingress.py asserts the
// identical constant for compute_edge_auth, so a unilateral change to
// key/message encoding or hex casing in either implementation fails one of
// the two suites instead of 403ing every proxied request in production.
test("edge-auth HMAC matches the origin implementation's golden vector", async () => {
  assert.equal(
    await expectedEdgeAuth("secret-token", "11111111-1111-4111-8111-111111111111"),
    "1d489d84d7a8dcec3ddef522064e6ee09269bb80fd3dc91cd62c4ebf1ab220b4",
  );
});

test("read fan-out stamps a request-id and a valid HMAC edge-auth header on every attempt", async () => {
  const forwarded = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (request) => {
    forwarded.push({
      origin: new URL(request.url).origin,
      requestId: request.headers.get("x-exomem-request-id"),
      edgeAuth: request.headers.get("x-exomem-edge-auth"),
    });
    return new Response("down", { status: 503 });
  };
  try {
    const env = edgeEnv(null);
    env.STATE_TOKEN = "coordinator-secret";
    const request = new Request("https://exomem.example.com/health/ready");
    await worker.fetch(request, env);

    assert.equal(forwarded.length, 2);
    const requestId = forwarded[0].requestId;
    assert.match(requestId, /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
    const expected = await expectedEdgeAuth("coordinator-secret", requestId);
    for (const attempt of forwarded) {
      assert.equal(attempt.requestId, requestId);
      assert.equal(attempt.edgeAuth, expected);
    }
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("mutation proxy stamps a valid HMAC edge-auth header alongside the request-id", async () => {
  const forwarded = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (request) => {
    forwarded.push({
      requestId: request.headers.get("x-exomem-request-id"),
      edgeAuth: request.headers.get("x-exomem-edge-auth"),
    });
    return new Response("ok", { status: 200 });
  };
  try {
    const env = edgeEnv("desktop");
    env.STATE_TOKEN = "coordinator-secret";
    const response = await worker.fetch(toolCall(), env);
    assert.equal(response.status, 200);
    assert.equal(forwarded.length, 1);
    const expected = await expectedEdgeAuth("coordinator-secret", forwarded[0].requestId);
    assert.equal(forwarded[0].edgeAuth, expected);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("edge-auth header is omitted (not thrown) when STATE_TOKEN is unset", async () => {
  const forwarded = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (request) => {
    forwarded.push(request.headers.get("x-exomem-edge-auth"));
    return new Response("ok", { status: 200 });
  };
  try {
    const env = edgeEnv("desktop");
    delete env.STATE_TOKEN;
    const response = await worker.fetch(toolCall(), env);
    assert.equal(response.status, 200);
    assert.deepEqual(forwarded, [null]);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("GET /__version rejects requests without the coordinator bearer token", async () => {
  const env = edgeEnv("desktop");
  env.STATE_TOKEN = "secret";
  const response = await worker.fetch(new Request("https://exomem.example.com/__version"), env);
  assert.equal(response.status, 401);
  assert.deepEqual(await body(response), { error: "unauthorized" });
});

test("GET /__version returns deploy identity and effective vars without leaking the secret", async () => {
  const env = edgeEnv("desktop");
  env.STATE_TOKEN = "secret";
  env.WORKER_GIT_SHA = "abc1234";
  const request = new Request("https://exomem.example.com/__version");
  request.headers.set("authorization", "Bearer secret");
  const response = await worker.fetch(request, env);
  assert.equal(response.status, 200);
  const payload = await body(response);
  assert.deepEqual(payload, {
    service: "exomem-ha-edge",
    git_sha: "abc1234",
    deployed_vars: {
      MCP_TOOL_TIMEOUT_MS: 15000,
      ORIGIN_TIMEOUT_MS: 2500,
      REQUIRE_COORDINATION: true,
      SUPPORTED_RUNTIME_CONTRACTS: "1",
      DESKTOP_REPLICA_ID: "desktop",
      LAPTOP_REPLICA_ID: "laptop",
      DESKTOP_ORIGIN: "https://desktop.example.com",
      LAPTOP_ORIGIN: "https://laptop.example.com",
    },
  });
  assert.equal(JSON.stringify(payload).includes("secret"), false);
});

test("GET /__version reports an unlabeled deploy when WORKER_GIT_SHA is not set", async () => {
  const env = edgeEnv("desktop");
  env.STATE_TOKEN = "secret";
  const request = new Request("https://exomem.example.com/__version");
  request.headers.set("authorization", "Bearer secret");
  const response = await worker.fetch(request, env);
  assert.equal(response.status, 200);
  assert.equal((await body(response)).git_sha, "unlabeled");
});

test("safe MCP initialization retains short-timeout fallback", async () => {
  const calls = [];
  const timeouts = [];
  const originalFetch = globalThis.fetch;
  const originalTimeout = AbortSignal.timeout;
  globalThis.fetch = async (request) => {
    const origin = new URL(request.url).origin;
    calls.push(origin);
    return new Response(origin.includes("desktop") ? "down" : "initialized", {
      status: origin.includes("desktop") ? 503 : 200,
    });
  };
  AbortSignal.timeout = (ms) => {
    timeouts.push(ms);
    return originalTimeout(60_000);
  };
  try {
    const request = mcp({ jsonrpc: "2.0", id: 1, method: "initialize", params: {} });
    const response = await worker.fetch(request, edgeEnv("desktop"));
    assert.equal(response.status, 200);
    assert.deepEqual(calls, ["https://desktop.example.com", "https://laptop.example.com"]);
    assert.deepEqual(timeouts, [2_500, 2_500]);
  } finally {
    globalThis.fetch = originalFetch;
    AbortSignal.timeout = originalTimeout;
  }
});
