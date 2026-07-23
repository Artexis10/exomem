# Design — deterministic edge ingress

## Context

Topology: two Windows machines each run the exomem service on `127.0.0.1:8765`
behind their own named cloudflared tunnel and per-machine hostname. The public
apex hostname is (must be) served exclusively by the HA worker
(`deploy/cloudflare-ha/src/worker.js`), which also hosts the lease coordinator
Durable Object. The worker proxies reads holder-first with fallback and routes
mutation-capable requests to the lease holder only.

The failure this change defends against: apex traffic reaching an origin
without passing through the worker (stale DNS binding to a tunnel, path-scoped
worker route, a second connector on a shared tunnel, or any future ingress
drift).

## Decision 1 — Edge-transit stamp

**Mechanism.** The worker computes `hex(HMAC_SHA256(STATE_TOKEN, request_id))`
(WebCrypto) and sets two headers on every proxied origin fetch — both the
read fan-out loop and `proxyMutationRequest`:

- `x-exomem-request-id`: already present on the mutation path; the read path
  now gets it too (fresh UUIDv4 when the client did not present a valid one).
- `x-exomem-edge-auth`: the HMAC over the request-id value.

**Key.** `STATE_TOKEN` on the worker is the same secret as
`EXOMEM_WRITER_LEASE_TOKEN` on the origins, so no new secret distribution is
needed. The raw token never travels on proxied requests; only the per-request
HMAC does.

**Enforcement point (origin).** ASGI middleware installed on the FastMCP app
and the REST facade. A request is refused when ALL hold:

1. lease coordination is enabled (`EXOMEM_WRITER_LEASE_URL` set) and a token
   is configured;
2. the request transited Cloudflare — detected by the presence of a `cf-ray`
   header. Local CLI/REST/localhost traffic never carries `cf-ray` and is
   exempt; anything arriving through a tunnel always does, so external
   clients cannot dodge the check;
3. the method is unsafe — anything other than `GET`/`HEAD`/`OPTIONS` (SSE and
   health GETs stay open so reads keep working during break-glass; the unsafe
   set covers every mutation including the capability-bound transfer `PUT`,
   which the worker also classifies as mutation-capable and stamps);
4. `x-exomem-edge-auth` is absent or does not verify against
   `x-exomem-request-id` (comparison via `hmac.compare_digest`).

Refusal: HTTP 403 with the standard OpError envelope, code `INGRESS_BYPASSED`,
message "request reached the origin without transiting the HA edge",
remediation "Public traffic must enter via the HA edge hostname; check DNS,
tunnel ingress, and worker route coverage, or set
EXOMEM_EDGE_STAMP_ENFORCE=0 to break glass." Refusals are logged content-free
with the request path and a counter; the readiness payload is unchanged.

**Why method-scope instead of parsing mutation-capability at the origin:** the
worker already stamps everything it proxies, so scoping enforcement by method
avoids re-implementing `isMutationCapableRequest` body-sniffing in ASGI while
covering every mutation (all unsafe methods, including the transfer `PUT`)
plus non-tool MCP POSTs — making bypass loud even for traffic that would not
have corrupted state.

**Kill switch.** `EXOMEM_EDGE_STAMP_ENFORCE=0` disables refusal (checks still
log). Default is enforce-when-lease-enabled. Standalone deployments (no lease
URL) are entirely unaffected.

**Replay considerations.** The stamp proves edge transit, not freshness; an
HMAC pair could be replayed by an actor who can already read proxied traffic
inside the trust boundary (cloudflared <-> localhost). That actor can reach
`127.0.0.1:8765` directly anyway, so no nonce/timestamp is added.

## Decision 2 — Edge provenance

`GET /__version` on the worker, gated by the same bearer check as the
coordinator endpoints (`authorized(request, env.STATE_TOKEN)`), returns:

```json
{
  "service": "exomem-ha-edge",
  "git_sha": "<env.WORKER_GIT_SHA or 'unlabeled'>",
  "deployed_vars": {
    "MCP_TOOL_TIMEOUT_MS": 60000,
    "ORIGIN_TIMEOUT_MS": 2500,
    "REQUIRE_COORDINATION": true,
    "SUPPORTED_RUNTIME_CONTRACTS": "1",
    "DESKTOP_REPLICA_ID": "desktop",
    "LAPTOP_REPLICA_ID": "laptop",
    "DESKTOP_ORIGIN": "https://...",
    "LAPTOP_ORIGIN": "https://..."
  }
}
```

No secrets are included (`STATE_TOKEN` never appears). Unauthenticated
requests get the existing `{"error":"unauthorized"}` 401 — which itself serves
as the "worker is in front" fingerprint for doctor.

Deploy labeling: `deploy/cloudflare-ha/deploy.ps1` (and a POSIX `deploy.sh`)
run `npx wrangler deploy --var WORKER_GIT_SHA:$(git rev-parse --short HEAD)`.
A bare `wrangler deploy` still works and reports `"git_sha": "unlabeled"`.

## Decision 3 — Doctor ingress conformance

New doctor section `edge-ingress` (skipped entirely when the lease is
disabled), using `EXOMEM_BASE_URL` as the public URL and the lease token for
auth:

1. `GET <base>/v1/vaults/<vault>/lease` **without** auth must return 401 with
   the worker's JSON shape — proves the worker fronts the apex for coordinator
   paths.
2. `GET <base>/__version` **with** auth must succeed — proves worker route
   coverage beyond `/v1/*` (a tunnel-direct apex would 404 here) — and its
   `deployed_vars` must satisfy: `MCP_TOOL_TIMEOUT_MS >= 60000`,
   `REQUIRE_COORDINATION` truthy, and this origin's own
   `EXOMEM_WRITER_LEASE_REPLICA_ID` present among the replica-id vars with an
   origin URL configured. Mismatch or `git_sha: "unlabeled"` -> warning;
   missing endpoint -> failure.
3. `GET <base>/health/ready` must report a `replica_id` equal to the
   coordinator's current lease holder (fetched via the lease status API) —
   proves holder-first read routing is intact.
4. Config lint: warn when `EXOMEM_WRITER_LEASE_TTL` is set below 30.

Failures render in the existing doctor report format with remediation lines
pointing at DNS binding / tunnel ingress / worker route as the three usual
suspects.

## Rollback

- Origin: `EXOMEM_EDGE_STAMP_ENFORCE=0` (env, no redeploy).
- Worker: previous deploy via `wrangler rollback`; `/__version` and the stamp
  headers are additive and ignored by pre-change origins.
- Doctor checks are read-only.

## Test strategy

- Worker (`deploy/cloudflare-ha/test/worker.test.mjs`): stamp present+valid on
  read-path and mutation-path proxied fetches; `/__version` auth gate, payload
  shape, secret exclusion; unlabeled fallback.
- Python: middleware unit tests — enforced refusal (cf-ray + POST + bad/absent
  stamp), pass-through (valid stamp; no cf-ray; GET; kill switch; lease
  disabled); `INGRESS_BYPASSED` terminal classification; doctor checks against
  a stubbed public endpoint (worker-shaped vs tunnel-shaped responses).
- Existing lease/worker suites stay green.
