/**
 * Exomem active/passive edge.
 *
 * A SQLite Durable Object holds writer leases and opaque client-encrypted OAuth
 * records. Normal requests are proxied to the current writer, with a bounded
 * health fallback to the other replica. Vault content is never persisted here.
 */

const json = (body, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json; charset=utf-8" },
  });

export class ExomemState {
  constructor(state) {
    this.state = state;
  }

  async fetch(request) {
    const url = new URL(request.url);
    if (url.pathname === "/lease") return this.lease(request);
    if (url.pathname.startsWith("/state/")) return this.sharedState(request, url.pathname.slice(7));
    return json({ error: "not found" }, 404);
  }

  async lease(request) {
    const now = Date.now() / 1000;
    if (request.method === "GET") {
      return json(await this.leaseStatus(now));
    }
    const operation = new URL(request.url).searchParams.get("operation");
    let body;
    try {
      body = await request.json();
    } catch {
      return json({ error: "invalid request" }, 400);
    }
    const replica = String(body.replica_id || "");
    if (!replica) return json({ error: "invalid request" }, 400);

    return this.state.storage.transaction(async (tx) => {
      const current = (await tx.get("lease")) || {
        holder: null,
        expires_at: null,
        fencing_token: 0,
      };
      const active = current.holder && current.expires_at > now;
      if (operation === "acquire") {
        const ttl = validTtl(body.ttl_seconds);
        if (!ttl) return json({ error: "invalid request" }, 400);
        if (active && current.holder !== replica) return json({ ...current, granted: false });
        const next = {
          holder: replica,
          expires_at: now + ttl,
          fencing_token:
            active && current.holder === replica
              ? current.fencing_token
              : current.fencing_token + 1,
        };
        await tx.put("lease", next);
        return json({ ...next, granted: true });
      }
      const token = Number(body.fencing_token);
      const valid = active && current.holder === replica && current.fencing_token === token;
      if (operation === "renew") {
        const ttl = validTtl(body.ttl_seconds);
        if (!ttl) return json({ error: "invalid request" }, 400);
        if (valid) {
          current.expires_at = now + ttl;
          await tx.put("lease", current);
        }
        return json({ ...normalizeLease(current, now), granted: Boolean(valid) });
      }
      if (operation === "release") {
        if (valid) {
          current.holder = null;
          current.expires_at = null;
          await tx.put("lease", current);
        }
        return json({ ...normalizeLease(current, now), granted: Boolean(valid) });
      }
      return json({ error: "unknown operation" }, 404);
    });
  }

  async leaseStatus(now) {
    const current = (await this.state.storage.get("lease")) || {
      holder: null,
      expires_at: null,
      fencing_token: 0,
    };
    const normalized = normalizeLease(current, now);
    if (current.holder && !normalized.holder) await this.state.storage.put("lease", normalized);
    return { ...normalized, granted: false };
  }

  async sharedState(request, operation) {
    let body;
    try {
      body = await request.json();
    } catch {
      return json({ error: "invalid request" }, 400);
    }
    const collection = body.collection == null ? "" : String(body.collection);
    const itemKey = (key) => `state\0${collection}\0${String(key)}`;
    const read = async (key) => {
      const stored = await this.state.storage.get(itemKey(key));
      if (!stored) return [null, null];
      const now = Date.now() / 1000;
      if (stored.expires_at != null && stored.expires_at <= now) {
        await this.state.storage.delete(itemKey(key));
        return [null, null];
      }
      return [
        stored.value,
        stored.expires_at == null ? null : Math.max(0, stored.expires_at - now),
      ];
    };
    try {
      if (operation === "get") return json({ result: (await read(body.key))[0] });
      if (operation === "ttl") return json({ result: await read(body.key) });
      if (operation === "put") {
        await this.state.storage.put(itemKey(body.key), stateValue(body));
        return json({ result: null });
      }
      if (operation === "delete") {
        const existed = (await this.state.storage.get(itemKey(body.key))) !== undefined;
        await this.state.storage.delete(itemKey(body.key));
        return json({ result: existed });
      }
      if (operation === "get-many" || operation === "ttl-many") {
        const values = await Promise.all(body.keys.map(read));
        return json({ result: operation === "get-many" ? values.map((v) => v[0]) : values });
      }
      if (operation === "put-many") {
        if (body.keys.length !== body.values.length) throw new Error("length mismatch");
        const entries = {};
        body.keys.forEach((key, index) => {
          entries[itemKey(key)] = stateValue({ ...body, value: body.values[index] });
        });
        await this.state.storage.put(entries);
        return json({ result: null });
      }
      if (operation === "delete-many") {
        const keys = body.keys.map(itemKey);
        const existing = await this.state.storage.get(keys);
        await this.state.storage.delete(keys);
        return json({ result: existing.size });
      }
      return json({ error: "unknown operation" }, 404);
    } catch {
      return json({ error: "invalid request" }, 400);
    }
  }
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const leaseMatch = url.pathname.match(/^\/v1\/vaults\/([^/]+)\/lease(?:\/([^/]+))?$/);
    const stateMatch = url.pathname.match(/^\/v1\/state\/([^/]+)\/([^/]+)$/);
    if (leaseMatch || stateMatch) {
      if (!authorized(request, env.STATE_TOKEN)) return json({ error: "unauthorized" }, 401);
      if (leaseMatch) {
        const id = env.EXOMEM_STATE.idFromName(`lease:${decodeURIComponent(leaseMatch[1])}`);
        const stub = env.EXOMEM_STATE.get(id);
        const target = new URL("https://state/lease");
        if (leaseMatch[2]) target.searchParams.set("operation", leaseMatch[2]);
        return stub.fetch(new Request(target, request));
      }
      const id = env.EXOMEM_STATE.idFromName(`state:${decodeURIComponent(stateMatch[1])}`);
      return env.EXOMEM_STATE.get(id).fetch(
        new Request(`https://state/state/${stateMatch[2]}`, request),
      );
    }

    const holder = await currentHolder(env);
    const replicas = holder === env.LAPTOP_REPLICA_ID
      ? [env.LAPTOP_ORIGIN, env.DESKTOP_ORIGIN]
      : [env.DESKTOP_ORIGIN, env.LAPTOP_ORIGIN];
    let lastResponse;
    for (const origin of replicas) {
      if (!origin) continue;
      try {
        const target = new URL(url.pathname + url.search, origin);
        const response = await fetch(new Request(target, request.clone()), {
          signal: AbortSignal.timeout(Number(env.ORIGIN_TIMEOUT_MS || 2500)),
          redirect: "manual",
        });
        lastResponse = response;
        if (response.status < 500) return response;
      } catch {
        // Try the passive replica. Its writer guard still fails closed until takeover.
      }
    }
    return lastResponse || json({ error: "both Exomem replicas are unavailable" }, 503);
  },
};

async function currentHolder(env) {
  const id = env.EXOMEM_STATE.idFromName(`lease:${env.VAULT_ID}`);
  const response = await env.EXOMEM_STATE.get(id).fetch("https://state/lease");
  return (await response.json()).holder;
}

function normalizeLease(lease, now) {
  if (lease.holder && lease.expires_at > now) return lease;
  return { holder: null, expires_at: null, fencing_token: Number(lease.fencing_token || 0) };
}

function validTtl(value) {
  const ttl = Number(value);
  return ttl > 0 && ttl <= 3600 ? ttl : null;
}

function stateValue(body) {
  if (!body.value || typeof body.value !== "object" || Array.isArray(body.value)) throw new Error("invalid value");
  const ttl = body.ttl == null ? null : Number(body.ttl);
  if (ttl != null && !(ttl > 0)) throw new Error("invalid ttl");
  return { value: body.value, expires_at: ttl == null ? null : Date.now() / 1000 + ttl };
}

function authorized(request, expected) {
  if (!expected) return false;
  return request.headers.get("authorization") === `Bearer ${expected}`;
}
