## Context

See proposal.md for motivation. Existing hosted cells already provide private registry-backed agent commands, signed release/compatibility artifacts, isolated persistent storage and lifecycle fencing. Substrate currently fetches the agent contract before every command and forwards through the public control hostname. Moving that same external round trip into a new process would not deliver the intended topology.

The companion Substrate change owns the activation/service/certification boundary and canonical gateway code. This change owns the new cell contract, cluster/edge wiring and reproducible launch acceptance. The current hosted health smoke establishes process/index health, not successful OAuth, meaningful recall or host-client continuity.

## Goals / Non-Goals

**Goals:** Retain existing isolation and infrastructure; make contract checking occur at execution; keep the gateway-to-cell hop private and nearby; make acceptance runnable by agents without continuous operator attendance.

**Non-Goals:** A full control-plane move, a separate friends runtime, generic DCR, per-cell public endpoints, broad retrieval redesign, new models, automatic marketplace approval, replacing Neon, or deleting existing provider branches.

## Decisions

### 1. Add a versioned command-time binding, not a cache of mutable readiness

Publish `contracts/hosted-agent-command-binding-v1.json` as the canonical cross-repository transport fixture. Add `POST /private/exomem/v2/agent/{surface_profile}/command/{command_name}` backed by the same leaf/registry and lifecycle boundary as the current v1 command route. This route version does not change the public MCP version or silently redefine the existing runtime-attestation protocol.

Require the existing service credential, cell, principal, protocol and request-ID context plus three bounded scalar headers: `x-exomem-expected-release`, `x-exomem-expected-command-fingerprint`, and `x-exomem-expected-contract-digest`. Profile comes from the validated route. Reject duplicate, missing, malformed or mismatched values before command argument coercion, vault access or leaf dispatch. Compute actual values from the running process's registered agent contract, never the request. Reuse the existing published schema-digest algorithm, including its deliberate exclusion of release and self-digest fields; release has its own comparison. Catalog/plugin compatibility digest remains checked in Substrate's signed candidate/fleet binding, not by the cell: its inputs include client packaging that is deliberately absent from the runtime image. Do not add packaging files to the cell merely to duplicate that check.

Advertise `agent-command-binding-v1` in the signed compatibility artifact. Substrate imports the canonical fixture and uses the new route only for approved candidates advertising it. Keep v1 and its current checks for existing adapters during rollout. A v2 mismatch never falls back to v1. Both routes share the same effective principal/cell idempotency namespace, mutation receipts, error codes and cancellation behavior. Contract validation and command admission use the same immutable process contract so the old GET/POST time-of-check gap disappears.

The private contract GET remains for registration, diagnostics and old approved runtimes; it is no longer required on the hot path for the new route. Adding an expected tuple to the existing unversioned behavior without a clear compatibility boundary was rejected because missing-header fallback could silently remove the intended guarantee.

### 2. Reuse the existing edge and cluster transport

The target path is: client → existing public MCP URL at the edge → shared gateway in the existing cluster → private Traefik service → mapped isolated cell. The control-plane website, OAuth endpoints and billing remain on their current deployment. Browser transfer routes are unchanged.

Use Substrate's native Vercel external rewrite for the exact canonical public MCP path, ahead of the old local route, pointing to a dedicated hostname on the existing Cloudflare tunnel. This is edge routing, not another Next function or authorization implementation. Exomem foundation owns only the new origin DNS/tunnel configuration; Substrate owns its rewrite. Preserve streaming, cancellation and method/status/header semantics without buffering or redirecting the public client. Disable external-rewrite caching explicitly (`x-vercel-enable-rewrite-caching: 0`) and return private/no-store on every MCP response. Public resource/issuer configuration stays explicit in the gateway. The origin accepts only gateway resource/health routes and applies the same OAuth checks even when addressed directly; it never exposes cell/admin paths. Do not forward its bearer into a cell.

Preflight the external rewrite's route precedence, streaming/timeout behavior and original-header handling before deployment. A missing permission or incompatible transport is a deployment gate with a specific operator action, not permission to move the whole website or change the public audience. Roll back by removing the MCP rewrite only. The previous Next adapter remains deployable over identical handler code. A Cloudflare Worker in front of the whole Vercel hostname was rejected: it adds code and may require changing the website's DNS proxy boundary. Native external rewrites are documented at https://vercel.com/docs/routing/rewrites; this implementation must prove the actual built route is edge forwarding rather than the old regional function.

Treat pre-authentication network-source throttling separately from authenticated principal limits. Ignore caller-controlled forwarded IP headers. Trust only ingress-overwritten metadata arriving from the network-policy-restricted tunnel/ingress path; Vercel egress may represent an aggregate source bucket, not the end user's IP. Size that bucket for the proxy and retain current per-identity SQL rate limits. Direct-origin requests still receive the same token/client/entitlement checks and cannot impersonate a different authenticated principal with headers.

Vercel documents a 120-second first-response/inter-chunk idle timeout, with active streams allowed to run longer (https://vercel.com/changelog/cdn-origin-timeout-increased-to-two-minutes). Acceptance must prove Authorization and MCP protocol/session header fidelity plus streaming, cancellation and reconnect through the actual external rewrite. Retain the much shorter canonical tool deadline; no supported flow can require a silently idle stream beyond the intermediary limit.

Use the existing ClusterIP Traefik service and configured control Host for gateway-to-cell requests. Substrate validates the stored mapped-cell endpoint then translates only its origin to this fixed local transport. No arbitrary URL or public header can select the private origin, Host or cell. HTTP on this existing internal hop is an explicit trusted-cluster boundary, not permission for internet HTTP: gateway egress explicitly allows the platform Traefik namespace/service on port 80 plus required DNS and database transport; gateway ingress is confined to the tunnel/ingress and health-probe boundary. Cells retain their existing private-ingress allowlist. Other workloads cannot directly access these boundaries. Service credentials and identity validation still protect the cell. A compromised cluster/node is outside the tenant isolation guarantee already made by this single-cluster alpha.

Add a pinned gateway image/deployment/service, resource limits, readiness/liveness and graceful termination to the platform chart. No Kubernetes API token is mounted. Bind only required database and credential-unwrapping secrets; do not copy Vercel's complete environment, provider-admin, email or billing keys. Configure bounded pools, scale and rate limits for five users before increasing replicas. Do not create another Kubernetes cluster, tunnel, tenant storage stack or distributed authorization cache.

### 3. Runtime, service and marketplace evidence remain distinct

The Substrate companion defines activation and certification transactions. Runtime activation still requires authentic candidate identity, current strict v2 matching evidence and the nonempty routable-set fence. Client artifact certification happens later against the active runtime. Existing canary authorization remains only a narrowly scoped operator preparation mechanism, never the normal customer path.

Activation ends internal-canary authority atomically while preserving independently verifiable artifact evidence. Acceptance after activation uses ordinary approved service authorization on the same synthetic tenant, not an extended/revived canary grant. Both replaying the old canary and certifying the preserved exact artifact are explicit integration checks; the first must fail and the second must remain possible.

Use one durable synthetic test tenant for repeated service acceptance. Reconnect and refresh that tenant without re-provisioning it. A second isolated fixture tenant proves cross-tenant denial; it is not a second production-serving architecture. Freeze runtime release identity during the acceptance window. Preserve backups and the current owner vault; never reset a live user vault to manufacture a clean client run.

### 4. Agent-run acceptance, with explicit limits on what automation proves

Add one resumable acceptance command with independent stages and a machine-readable plus concise human report. Use run IDs, exact release/contract hashes and fixture manifests. A rerun continues outstanding checks or creates a new isolated fixture set; it never repeats an irreversible action merely because the agent lost context. Cleanup targets only manifests owned by that run and preserves evidence needed for diagnosis/recovery.

Default unit/SQL/protocol tests use local disposable state, not a new paid cloud database branch per agent or pull request. Cloud acceptance uses explicitly reserved test tenants and records allocated resources and their owner/expiry. No preview deployment or database branch is created implicitly by the acceptance runner. Provider cleanup remains a separately authorized operation.

The acceptance matrix covers:

- Real OAuth authorization/consent through the normal application boundary, then MCP initialize/tools/list and first capture. A scripted standards client exercises the public protocol; database-inserted tokens do not prove login.
- Semantic recall of a run-specific fact with paraphrased wording and a resolvable citation, using a realistically sized synthetic corpus and retrieval dependencies enabled. Counted index rows are not recall evidence.
- Fresh client process/conversation, rotating token continuity after the configured access-token lifetime (currently 15 minutes), replay rejection, and successful service use after at least one fleet credential renewal window (currently one hour). These waits run unattended while other stages proceed.
- Revocation, suspension, wrong audience/client, lifecycle races, concurrent sentinel isolation, lost-response idempotent retry and database/cell outage separation.
- Non-destructive backup verification plus a governed isolated restore drill proving recoverability without overwriting the source tenant.
- Genuine supported-host runs for any client being certified. Browser automation and available host tools should perform them; platform consent, CAPTCHA/MFA or unavailable host automation is an explicit blocked stage, not a fake pass or an endless request for operator observation.

Ask the operator once for a precise irreducible action and checkpoint immediately. Continue independent stages, then present one consolidated decision report. Absence of a host certification does not prevent an invite-only service release whose supported custom-client acceptance has passed, but no untested host receives a support/certification claim.

### 5. Measure fast rather than declaring fast

Before optimization, record authenticated old-path timings with the same corpus, location and workload as the new path. Report cold/warm initialize, tool listing, durable small capture and citation-bearing recall separately. Exclude model thinking and time spent by a human on email/consent, but report those boundaries clearly. Include gateway overhead and database/private-cell stage timing without logging arguments or content.

Initial alpha performance acceptance: at five concurrent clients across two explicitly reserved synthetic tenants, warm p95 initialize/tool listing ≤500 ms, small durable capture and citation-bearing recall ≤1 s, and authenticated cold initialize ≤2 s from the chosen European test vantage point. Collect at least 100 warm samples per operation and 20 explicitly reset cold runs; report p50/p95/errors and the exact reset method. Use at least 1,000 synthetic notes and 10 MiB of varied text per test cell; keep fixture generation deterministic and content public-safe. These are launch targets, not measured results, a five-cell capacity claim or a worldwide SLA. Live tests reuse reserved capacity; they never displace an owner or implicitly allocate five new paid cells.

Measure durable acknowledgement separately from background graph/embedding convergence. A write may acknowledge after its required durable mutation boundary without waiting for unrelated heavy work; the acceptance runner waits for declared indexing convergence before testing semantic recall. If a target misses, retain the failing report and identify the measured stage. Do not introduce eventual authorization, drop compatibility checks or claim performance from the unauthenticated 401 measurement.

## Risks / Trade-offs

- Extra process adds operations → reuse one canonical handler, native edge rewrite, existing tunnel/ingress and declarative deployment; retain exact-path rollback.
- Private HTTP trusts the cluster network → strict allowlisted origin, NetworkPolicy and cell credentials; reject arbitrary host/path rewrites. Do not advertise protection from a compromised host node.
- Database region or compute may dominate → capture SQL timings and deployment region evidence before resizing or migrating anything.
- Some host platforms need one-time human consent → checkpoint the exact missing action and continue all independent tests; never depend on continuous attendance.
- Tight same-day goal encourages skipping long-window evidence → start unattended continuity/renewal checks early and do other work concurrently; mark incomplete evidence blocked/pending rather than passed.

## Migration Plan

1. Pin the cross-repository command-binding fixture and add negative tests before implementing the additive cell route.
2. Publish/deploy the new signed candidate with v1 retained. The Substrate runtime-only activation workflow verifies and activates it.
3. Deploy the gateway privately with least-privilege secrets and fixed internal routing. Validate connection pooling, probes, drain, same-cell retries and blocked direct access.
4. Apply only the public MCP edge override after verifying route ownership; run admission, transport, isolation and latency acceptance. Keep runtime identity fixed while continuity/renewal windows complete.
5. Run genuine host certification separately and record the publication decision per platform. Preserve the acceptance tenant for future regression runs.

Rollback the public edge override first if gateway transport is faulty. Keep token records and public URL unchanged. Roll back a cell only through the existing signed runtime/fleet procedure; do not remove v1 compatibility until the old adapter rollback window is retired. Sync/archive the paired OpenSpec changes only after their non-optional tasks have actual implementation, verification and delivery evidence.
