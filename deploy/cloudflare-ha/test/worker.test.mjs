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

test("edge rejects unauthenticated coordinator access", async () => {
  const env = { STATE_TOKEN: "secret", EXOMEM_STATE: { idFromName: (name) => name, get: () => { throw new Error("must not reach state"); } } };
  const response = await worker.fetch(post("/v1/vaults/main/lease/acquire", { replica_id: "desktop", ttl_seconds: 30 }), env);
  assert.equal(response.status, 401);
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
  const timeouts = [];
  const originalFetch = globalThis.fetch;
  const originalTimeout = AbortSignal.timeout;
  globalThis.fetch = async (request) => {
    calls.push(new URL(request.url).origin);
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
    assert.deepEqual(timeouts, [15_000]);
  } finally {
    globalThis.fetch = originalFetch;
    AbortSignal.timeout = originalTimeout;
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
