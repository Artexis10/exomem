import assert from "node:assert/strict";
import test from "node:test";

import worker, { ExomemState, isMcpToolCall } from "../src/worker.js";

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

const edgeEnv = (holder = "desktop") => ({
  VAULT_ID: "personal-main",
  DESKTOP_REPLICA_ID: "desktop",
  LAPTOP_REPLICA_ID: "laptop",
  DESKTOP_ORIGIN: "https://desktop.example.com",
  LAPTOP_ORIGIN: "https://laptop.example.com",
  ORIGIN_TIMEOUT_MS: "2500",
  MCP_TOOL_TIMEOUT_MS: "15000",
  EXOMEM_STATE: {
    idFromName: (name) => name,
    get: () => ({
      fetch: async () => new Response(JSON.stringify({ holder }), {
        headers: { "content-type": "application/json" },
      }),
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

test("no-holder tool call probes and selects exactly one healthy origin", async () => {
  const calls = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (request) => {
    const url = new URL(request.url);
    calls.push(url.href);
    if (url.pathname === "/.well-known/oauth-protected-resource/mcp") {
      return new Response("", { status: url.origin.includes("laptop") ? 200 : 503 });
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
      "/.well-known/oauth-protected-resource/mcp",
      "/.well-known/oauth-protected-resource/mcp",
    ]);
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
