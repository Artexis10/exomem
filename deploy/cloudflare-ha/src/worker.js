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
    if (url.pathname === "/admission") return this.admission(request);
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
        if (active && current.holder === replica && current.admission) {
          next.admission = current.admission;
        }
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
          delete current.admission;
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

  async admission(request) {
    if (request.method !== "POST") return json({ error: "method not allowed" }, 405);
    let body;
    try {
      body = await request.json();
    } catch {
      return json({ error: "invalid request" }, 400);
    }
    const holder = String(body.holder || "");
    const fencingToken = Number(body.fencing_token);
    const readiness = body.readiness;
    if (
      !holder
      || !Number.isInteger(fencingToken)
      || !readiness
      || typeof readiness !== "object"
      || Array.isArray(readiness)
    ) {
      return json({ error: "invalid request" }, 400);
    }
    const now = Date.now() / 1000;
    return this.state.storage.transaction(async (tx) => {
      const current = (await tx.get("lease")) || {
        holder: null,
        expires_at: null,
        fencing_token: 0,
      };
      const matches = current.holder === holder
        && current.expires_at > now
        && current.fencing_token === fencingToken;
      if (!matches) return json({ stored: false, error: "lease changed" }, 409);
      current.admission = {
        holder,
        fencing_token: fencingToken,
        readiness,
        admitted_at: now,
      };
      await tx.put("lease", current);
      return json({ stored: true });
    });
  }

  async sharedState(request, operation) {
    let body;
    try {
      body = await request.json();
    } catch {
      return json({ error: "invalid request" }, 400);
    }
    if (!body || typeof body !== "object" || Array.isArray(body)) {
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
      if (operation === "put-if-absent") {
        if (!Object.hasOwn(body, "key")) throw new Error("missing key");
        const key = itemKey(body.key);
        const value = stateValue(body);
        const created = await this.state.storage.transaction(async (tx) => {
          const stored = await tx.get(key);
          const now = Date.now() / 1000;
          if (stored && (stored.expires_at == null || stored.expires_at > now)) {
            return false;
          }
          if (stored) await tx.delete(key);
          await tx.put(key, value);
          return true;
        });
        return json({ result: created });
      }
      if (operation === "list-keys") {
        const prefix = itemKey("");
        const keys = await this.state.storage.transaction(async (tx) => {
          const entries = await tx.list({ prefix });
          const now = Date.now() / 1000;
          const expired = [];
          const live = [];
          for (const [key, stored] of entries) {
            if (stored.expires_at != null && stored.expires_at <= now) expired.push(key);
            else live.push(key.slice(prefix.length));
          }
          if (expired.length) await tx.delete(expired);
          return live.sort();
        });
        return json({ result: keys });
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

    const lease = await currentLease(env);
    const holder = lease.holder;
    if (await isMutationCapableRequest(request)) {
      return proxyMutationRequest(request, env, lease);
    }

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

export async function isMcpToolCall(request) {
  const url = new URL(request.url);
  if (url.pathname !== "/mcp" || request.method !== "POST") return false;
  let payload;
  try {
    payload = await request.clone().json();
  } catch {
    // A malformed MCP POST is ambiguous. Treat it as a tool call so the edge
    // never fans an unreadable body out to two origins.
    return true;
  }
  const messages = Array.isArray(payload) ? payload : [payload];
  return messages.some((message) => message && message.method === "tools/call");
}

export async function isMutationCapableRequest(request) {
  if (await isMcpToolCall(request)) return true;
  if (["GET", "HEAD", "OPTIONS"].includes(request.method)) return false;
  const pathname = new URL(request.url).pathname;
  // Non-tool MCP messages retain their compatibility fallback. Every other
  // unsafe method is single-origin by default, including REST/lifecycle POSTs
  // and the capability-bound public transfer PUT.
  return pathname !== "/mcp";
}

async function proxyMutationRequest(request, env, lease) {
  request = withMutationRequestId(request);
  const requestId = request.headers.get("x-exomem-request-id");
  const shortTimeout = Number(env.ORIGIN_TIMEOUT_MS || 2500);
  // 60s, not 15s: a governed write validates the draft against the full corpus
  // and re-validates under the creation lock, measured at 12-45s warm on the
  // 2026-07 production corpus (2.4k pages). Abandoning at 15s guaranteed the
  // origin kept committing after the edge stopped waiting — acknowledgement
  // loss on nearly every write. 60s stays under Cloudflare's ~100s proxy cap.
  const toolTimeout = Number(env.MCP_TOOL_TIMEOUT_MS || 60000);
  const holder = lease.holder;
  let candidate = candidateForHolder(env, holder);

  if (holder && !candidate) {
    return json({ error: "active Exomem replica is not configured" }, 503);
  }
  if (holder) {
    if (!admissionEligible(lease.admission, env, holder, lease.fencing_token)) {
      candidate = await probeCandidateReadiness(candidate, env, shortTimeout);
      if (!candidate) {
        return json({ error: "active Exomem replica is not runtime-ready" }, 503);
      }
      const stored = await recordAdmission(env, lease, candidate.readiness);
      if (!stored) return json({ error: "writer lease changed during runtime admission" }, 503);
    }
  } else {
    candidate = await selectEligibleOrigin(env, shortTimeout);
    if (!candidate) return json({ error: "both Exomem replicas are ineligible" }, 503);
  }

  try {
    console.log(JSON.stringify({
      event: "mutation_proxy",
      request_id: requestId,
      origin: candidate.origin,
    }));
    return await proxyOnce(request, candidate.origin, toolTimeout);
  } catch {
    // Never replay an ambiguous mutation-capable request. The origin may have
    // committed after the edge stopped waiting; cross-replica replay turns a
    // transport timeout into duplicate governed state.
    console.warn(JSON.stringify({
      event: "mutation_timeout",
      request_id: requestId,
      origin: candidate.origin,
    }));
    return json({
      error: "active Exomem mutation-capable request did not complete at the edge",
      request_id: requestId,
    }, 504);
  }
}

function withMutationRequestId(request) {
  const headers = new Headers(request.headers);
  const presented = String(headers.get("x-exomem-request-id") || "").trim();
  const requestId = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(presented)
    ? presented
    : crypto.randomUUID();
  headers.set("x-exomem-request-id", requestId);
  return new Request(request, { headers });
}

function candidateForHolder(env, holder) {
  if (holder === env.DESKTOP_REPLICA_ID && env.DESKTOP_ORIGIN) {
    return { origin: env.DESKTOP_ORIGIN, replicaId: env.DESKTOP_REPLICA_ID };
  }
  if (holder === env.LAPTOP_REPLICA_ID && env.LAPTOP_ORIGIN) {
    return { origin: env.LAPTOP_ORIGIN, replicaId: env.LAPTOP_REPLICA_ID };
  }
  return null;
}

function configuredCandidates(env) {
  return [
    env.DESKTOP_ORIGIN && env.DESKTOP_REPLICA_ID
      ? { origin: env.DESKTOP_ORIGIN, replicaId: env.DESKTOP_REPLICA_ID }
      : null,
    env.LAPTOP_ORIGIN && env.LAPTOP_REPLICA_ID
      ? { origin: env.LAPTOP_ORIGIN, replicaId: env.LAPTOP_REPLICA_ID }
      : null,
  ].filter(Boolean);
}

async function selectEligibleOrigin(env, timeoutMs) {
  const checked = await Promise.all(
    configuredCandidates(env).map((candidate) => probeCandidateReadiness(candidate, env, timeoutMs)),
  );
  return checked.find(Boolean) || null;
}

async function probeCandidateReadiness(candidate, env, timeoutMs) {
  try {
    const target = new URL("/health/ready", candidate.origin);
    const response = await fetch(new Request(target), {
      signal: AbortSignal.timeout(timeoutMs),
      redirect: "manual",
    });
    if (response.status !== 200) return null;
    const readiness = await response.json();
    const evaluation = evaluateReadiness(readiness, env, candidate.replicaId);
    return evaluation.eligible ? { ...candidate, readiness } : null;
  } catch {
    return null;
  }
}

export function evaluateReadiness(readiness, env, expectedReplicaId) {
  if (!readiness || typeof readiness !== "object" || Array.isArray(readiness)) {
    return { eligible: false, reason: "invalid_readiness_payload" };
  }
  if (readiness.status !== "ready" || readiness.service !== "exomem") {
    return { eligible: false, reason: "runtime_not_ready" };
  }
  if (typeof readiness.release !== "string" || !readiness.release) {
    return { eligible: false, reason: "release_identity_missing" };
  }
  const runtimeContract = Number(readiness.runtime_contract);
  if (!Number.isInteger(runtimeContract) || !supportedRuntimeContracts(env).has(runtimeContract)) {
    return { eligible: false, reason: "unsupported_runtime_contract" };
  }
  const requiredTransport = env.REQUIRED_RUNTIME_TRANSPORT || "streamable-http-stateless";
  if (readiness.transport !== requiredTransport) {
    return { eligible: false, reason: "unsupported_transport" };
  }
  if (readiness.replica_id !== expectedReplicaId) {
    return { eligible: false, reason: "replica_identity_mismatch" };
  }
  if (readiness.takeover_eligible !== true) {
    return { eligible: false, reason: "takeover_ineligible" };
  }
  const coordination = readiness.coordination;
  if (requireCoordination(env)) {
    if (!coordination || coordination.enabled !== true) {
      return { eligible: false, reason: "coordination_required" };
    }
    if (coordination.coordinator_healthy !== true) {
      return { eligible: false, reason: "coordinator_unavailable" };
    }
  }
  return { eligible: true, reason: null };
}

function supportedRuntimeContracts(env) {
  const values = String(env.SUPPORTED_RUNTIME_CONTRACTS || "1")
    .split(",")
    .map((value) => Number(value.trim()))
    .filter(Number.isInteger);
  return new Set(values);
}

function requireCoordination(env) {
  return !["0", "false", "no", "off"].includes(
    String(env.REQUIRE_COORDINATION ?? "true").trim().toLowerCase(),
  );
}

function admissionEligible(admission, env, holder, fencingToken) {
  if (!admission || admission.holder !== holder || admission.fencing_token !== fencingToken) {
    return false;
  }
  return evaluateReadiness(admission.readiness, env, holder).eligible;
}

async function recordAdmission(env, lease, readiness) {
  const id = env.EXOMEM_STATE.idFromName(`lease:${env.VAULT_ID}`);
  const response = await env.EXOMEM_STATE.get(id).fetch(new Request("https://state/admission", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      holder: lease.holder,
      fencing_token: lease.fencing_token,
      readiness: readinessSummary(readiness),
    }),
  }));
  return response.status === 200;
}

function readinessSummary(readiness) {
  return {
    status: readiness.status,
    service: readiness.service,
    release: readiness.release,
    runtime_contract: readiness.runtime_contract,
    transport: readiness.transport,
    replica_id: readiness.replica_id,
    coordination: {
      enabled: readiness.coordination?.enabled === true,
      role: readiness.coordination?.role || "unknown",
      coordinator_healthy: readiness.coordination?.coordinator_healthy === true,
    },
    takeover_eligible: readiness.takeover_eligible === true,
    reasons: Array.isArray(readiness.reasons) ? readiness.reasons.map(String).slice(0, 8) : [],
  };
}

function proxyOnce(request, origin, timeoutMs) {
  const source = new URL(request.url);
  const target = new URL(source.pathname + source.search, origin);
  return fetch(new Request(target, request.clone()), {
    signal: AbortSignal.timeout(timeoutMs),
    redirect: "manual",
  });
}

async function currentLease(env) {
  const id = env.EXOMEM_STATE.idFromName(`lease:${env.VAULT_ID}`);
  const response = await env.EXOMEM_STATE.get(id).fetch(new Request("https://state/lease"));
  return response.json();
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
  return request.headers.get("authorization") === `Bearer ${String(expected).trim()}`;
}
