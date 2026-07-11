import assert from "node:assert/strict";
import test from "node:test";

import worker, { ExomemState } from "../src/worker.js";

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
