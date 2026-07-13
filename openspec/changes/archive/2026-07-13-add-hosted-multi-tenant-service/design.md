## Context

Exomem is currently a single-vault runtime by construction. Startup resolves one
`EXOMEM_VAULT_PATH`, loads one schema and project-key set, starts one watcher and
media worker, and binds every MCP/REST/CLI command leaf to that vault. That is a
useful isolation boundary, not an obstacle to work around.

The hosted product therefore has two planes:

```text
Claude / ChatGPT / browser
          |
          v
Substrate hosted edge and control plane
  public auth, tenant registry, entitlements, Paddle, UI, orchestration
          |
          | authenticated private command/transfer contract
          v
one isolated Exomem cell per tenant
  one process/container + one vault + one state root + one secret set
          |
          v
canonical Markdown and original media
  rebuildable SQLite/search/worker sidecars remain tenant-local
```

The public service is multi-tenant at the product/control-plane level. The
Exomem data plane remains deliberately single-tenant. A request is never handled
by changing a process-global vault pointer or by adding tenant rows to the
canonical knowledge store.

This also corrects an older hosted-design shorthand that treated SQLite as the
vault. Exomem's durable source of truth is the tenant's Markdown, original media,
governed support files, and portable review state. Embedding, lexical, graph,
reference, claim, deferred-index, and media-job databases are derived operating
data and can be rebuilt.

Hosted demand is initially invite-sized and personal rather than team-oriented.
That makes physical isolation and simple operations more valuable than maximizing
tenant density. The design must still establish contracts that can be automated,
tested adversarially, and consumed by the companion Substrate product change.

## Goals / Non-Goals

**Goals:**

- Preserve the existing single-vault runtime as the tenant data-plane cell.
- Make every mutation against one vault serializable across request threads,
  CLI/REST/MCP entry points, uploads, snapshots, and background writer processes.
- Provide a fail-closed hosted-cell mode with explicit vault/state/log/secret
  boundaries and no local `.env` override.
- Define a versioned, registry-derived gateway-to-cell contract that preserves
  command semantics, error envelopes, and bounded retry/idempotency behavior.
- Ensure authenticated identity resolves to exactly one cell without accepting a
  tenant selector from any public request field.
- Provide a portable, integrity-checked snapshot/export and restore-preparation
  path for canonical Markdown and media.
- Keep hosted query text and vault paths out of provider operational logs.
- Keep optional embeddings and media workers entitlement-controlled,
  resource-bounded, and soft-failing to lexical recall and durable capture.
- Give Substrate a precise runtime contract for the first invite-to-useful-recall
  vertical slice without moving account, billing, or UI concerns into Exomem.

**Non-Goals:**

- Same-process multi-vault dispatch, a shared tenant content database, or
  row-level tenant isolation inside Exomem.
- Teams, shared vaults, roles within a vault, CRDT collaboration, or public
  knowledge sharing.
- A hosted chat UI or server-side reasoning model. Hosted inference remains
  deterministic measurement/transduction only: embeddings, reranking, OCR, ASR,
  CLIP, diarization, and frozen captioning.
- Implementing customer identity, public OAuth, Paddle checkout/webhooks,
  entitlement accounting, Exomem Home, or provisioning orchestration in this
  repository. Those belong to the companion Substrate change.
- Claiming zero-knowledge or end-to-end encryption. The service must read
  plaintext in memory to search and index it; hosted storage is encrypted at rest
  and owner-scoped.
- Optimizing for thousands of warm cells before the alpha establishes real load,
  cost, and latency data.

## Decisions

### 1. The deployment unit is an isolated single-vault cell

Each hosted tenant receives one Exomem process/container with:

- an opaque immutable `cell_id` unrelated to email, display name, or URL slug;
- one dedicated vault mount;
- one separate state root for locks, idempotency data, and worker state;
- one separate operational log root;
- unique cell service and transfer credentials; and
- explicit resource and feature limits.

The process sees no other tenant's vault or state mount. The public gateway does
not pass a filesystem path to the cell. A cell may share immutable application
images and model-weight caches, but it must not share writable vault, state,
logs, SQLite sidecars, or secret files.

**Why:** this reuses the strongest current boundary and turns a routing defect
into a failed request rather than cross-tenant filesystem access. It also keeps
export, restore, deletion, release pinning, and rollback tenant-sized.

**Alternatives considered:**

- *One FastMCP process with a request-scoped vault root.* Rejected for the first
  service: runtime workers, writer-lease/idempotency managers, configuration,
  query logs, readiness state, and some caches are process-global. Auditing every
  current and future global for tenant safety is a worse security boundary than
  process isolation.
- *Shared Postgres/object rows with tenant IDs.* Rejected because it replaces the
  owned-Markdown model and creates a second business-logic/storage implementation.
- *One manually configured public instance per user.* Useful as a prototype, but
  rejected as the product contract because it lacks shared auth, automatic
  lifecycle management, and a stable public gateway.

### 2. Substrate owns the shared control plane and public product

Ownership is explicit so neither repository grows a half-control-plane.

Exomem owns:

- process-safe vault mutation coordination;
- hosted-cell startup validation and private readiness;
- the private registry-backed command and transfer surface;
- a versioned gateway/cell protocol and conformance fixtures;
- canonical snapshot/export and restore-preparation helpers;
- local cell feature gates and resource-bounded degradation; and
- isolation, concurrency, retry, redaction, and portability tests at the cell
  boundary.

The companion Substrate OpenSpec change owns:

- invites, accounts, sessions, consumer OAuth, and the public MCP gateway;
- the immutable account-to-cell registry and routing implementation;
- Exomem Home, friendly capture, Connections, Review integration, and Account UI;
- Paddle products/prices, checkout, webhooks, customer portal, reconciliation,
  complimentary alpha access, and internal entitlement state;
- cell provisioning/scheduling, release rollout, health remediation, quotas, and
  suspension;
- tenant KMS/encryption integration, encrypted backup object storage, retention,
  and destructive deletion orchestration; and
- product analytics, support tooling, SLOs, and operator audit trails.

Paddle MCP is an operator/development convenience for the Substrate billing
lane. It is not a production dependency of Exomem cells or their gateway
contract.

Deployment infrastructure supplies the private network, container isolation,
volume encryption, secret injection, object storage, and ingress policy. Exomem
defines readiness checks that detect unsafe configuration; it does not attempt to
be its own scheduler or KMS.

### 3. Hosted-cell mode is explicit and fails closed

Hosted operation uses an explicit mode rather than inferring safety from the
presence of a REST key. The mode requires an opaque cell ID, an absolute vault
root, a distinct absolute state root, a distinct log root, and a cell service
credential. Startup must:

1. skip `load_dotenv` entirely so a shared working-directory `.env` cannot
   redirect multiple cells to the same vault;
2. resolve and validate the vault and state roots, rejecting overlap, unexpected
   symlinks, missing scaffold files, shared writable state, or paths outside the
   assigned mounts;
3. refuse consumer GitHub OAuth, Cloudflare Access, public Studio configuration,
   or other local-personal ingress assumptions in cell mode;
4. disable raw query logging and usage-derived access logs by default;
5. default embeddings, CLIP, ASR/OCR, diarization, and media workers off unless
   the provisioned cell policy enables them; and
6. expose only service-authenticated private routes on the cell network.

The private readiness response is deliberately content-free. It reports protocol
version, release, opaque cell ID, configuration validity, mutation-lock health,
canonical vault availability, derived-index readiness/degradation, worker state,
and whether the cell can accept reads and writes. It never reports vault paths,
queries, filenames, account identity, secrets, or content counts that would
become a side channel.

Ordinary local `exomem` startup remains unchanged. Hosted mode is an additive
entry point/configuration profile, not a new default.

**Alternative considered:** reuse the existing public HTTP mode with GitHub
OAuth and a different username per process. Rejected because non-technical users
should not need GitHub, it leaves public auth duplicated per cell, and it gives
the cell no trustworthy hosted lifecycle contract.

### 4. All canonical mutations share one process-safe coordinator

`batch_atomic_write` remains the atomic file-replacement primitive, but it is not
a transaction boundary for two concurrently planned mutations. Filename
selection, append-only existence checks, source back-references, `index.md`, and
`log.md` are all read-modify-write operations and therefore require serialization
around the full operation.

The runtime adds a `VaultMutationCoordinator` scoped by the resolved vault
identity. It combines a process-local reentrant lock with an OS-backed exclusive
lock file under the cell state root. The state-root location keeps lock files out
of vault sync, exports, and backups while still coordinating the HTTP process and
media child processes.

Every mutating command follows one path:

1. identify the command as mutating from the command registry;
2. acquire the local and OS mutation guard with a bounded wait;
3. revalidate writer authority when the optional multi-host lease is enabled;
4. execute retry/idempotency registration and the complete read-plan-write leaf
   while holding the guard;
5. commit canonical files atomically and update synchronous derived indexes;
6. persist the result, release the OS guard, then the local guard; and
7. perform non-writing notifications after release where their ordering is not
   observable as canonical state.

MCP, REST, and CLI continue through `invoke_command`. Upload/preserve, snapshot,
restore preparation, and any hand-registered mutation must call the same
coordinator rather than only checking the writer lease. Bypassing the coordinator
in hosted mode is a test failure.

Media workers may do expensive extraction outside the guard, but must acquire it
before creating/replacing a Markdown sidecar, scene-frame artifact, or related
index state. They must re-read the target and use an expected hash under the lock
so a user edit made during extraction is not overwritten. Hosted media workers
remain default-off until this conformance path is implemented and tested.

The guard queues normal personal-workload bursts. A bounded timeout returns a
stable retryable `MUTATION_BUSY` error rather than running unlocked. OS lock
ownership is released automatically on process death; stale metadata is
diagnostic only and never treated as authority.

**Alternatives considered:**

- *Lock only `batch_atomic_write`.* Rejected because the stale reads and filename
  choices happen before it.
- *A `threading.Lock` in the HTTP server.* Rejected because uploads and media
  child processes can write outside that thread boundary.
- *Optimistic retries on every touched file.* Viable later for greater write
  parallelism, but substantially more invasive than a per-vault serial queue and
  unnecessary for personal alpha workloads.

### 5. The gateway forwards commands; it never chooses vault paths

The public MCP gateway is implemented in Substrate but consumes a versioned
contract published by Exomem. Tool names, parameter schemas, read/write metadata,
and error shapes are derived from Exomem's command registry. The gateway must not
maintain hand-copied tool definitions or reimplement operation validation.

For every public call the gateway:

1. verifies the public session/token and obtains an immutable account subject;
2. resolves that subject to exactly one active tenant and cell using server-side
   control-plane state;
3. checks the internal entitlement and suspension state before forwarding;
4. selects the private cell endpoint and unique service credential from the
   tenant registry;
5. forwards only a known command name, validated arguments, request ID, protocol
   version, and retry context; and
6. returns the cell's structured result/error without weakening its governance
   or filesystem checks.

The public API has no `tenant_id`, `cell_id`, vault path, internal endpoint, or
service credential parameter. Tenant-looking values in bodies, query strings,
paths, or client-supplied headers are rejected or ignored; they never influence
routing. The cell receives the gateway's service identity, not the user's raw
OAuth bearer token.

The cell accepts private requests only after constant-time validation of its
unique service credential. Caller-supplied identity/retry headers are honored
only on that authenticated private channel. A service credential for one cell
must fail against every other cell.

The gateway preserves current bounded MCP retry behavior by sending a stable,
credential-safe retry scope or explicit idempotency key. The cell namespaces it
by opaque cell identity and command digest. Replaying the same key and payload
returns the stored result; reusing it for different input fails. The same public
key used against two tenants cannot collide because tenants have distinct cells,
state roots, and namespace prefixes.

Protocol negotiation is explicit. A gateway declares a supported contract
version; a cell either accepts it or returns a non-content-bearing incompatibility
error. Releases may add optional response fields, but removing commands,
parameters, or error codes requires a coordinated gateway/cell rollout.

### 6. Public transfers are tenant-bound at the gateway

The current local short-lived transfer token signs only an operation and expiry.
That is safe for one fixed-vault server but is not a public multi-tenant token.

For hosted operation, `transfer_artifact` is intercepted by the public gateway.
The gateway issues a short-lived token or opaque grant bound to:

- public subject and immutable tenant/cell identity;
- `upload` or `download` operation and hosted-transfer audience;
- issued-at and expiry times;
- a unique `jti`/grant ID; and
- any applicable size, media, or tier limit.

The hosted upload/download endpoint verifies the grant, derives the cell from the
verified claim, checks current entitlement/suspension state, and streams to the
private cell with that cell's service credential. URL paths and multipart fields
cannot override the resolved tenant. Private cell transfer credentials are never
returned to clients.

For the first release, replay resistance is provided by short TTL, `jti` audit,
append-only filename conflicts, and bounded request size. One-time grant
consumption may be added in Substrate without changing the cell contract.

### 7. Snapshots contain canonical portable data, not a running cell image

The cell exposes a snapshot/export helper and restore-preparation helper; the
Substrate orchestrator decides when to invoke them and where encrypted archives
are retained.

A snapshot proceeds as follows:

1. mark the cell draining so the gateway starts no new mutations;
2. pause background writers and wait for in-flight work to reach a safe point;
3. acquire the same exclusive vault mutation guard used by normal writes;
4. walk the vault without following symlinks and build a deterministic manifest;
5. copy eligible files into a staging archive outside the vault;
6. record relative path, byte size, SHA-256, artifact class, snapshot schema,
   Exomem release, and generation time in the manifest;
7. verify the archive against the manifest before publishing it; and
8. release the guard and resume or stop the cell as requested.

The portable payload includes user-authored/captured Markdown, original media and
attachments, the shipped/adapted schema, governed indexes and activity log,
recoverable trash, media Markdown sidecars, and `.review-state.json`. The phrase
"exclude logs" means provider operational logs under the cell log root; it does
not exclude the governed `Knowledge Base/log.md` file.

The payload excludes secrets, service credentials, OAuth/session state,
idempotency databases, lock files, temporary/backup files, provider logs,
voice-profile/model state, generated scene-frame directories, and rebuildable
SQLite/WAL/SHM sidecars including embeddings, CLIP, lexical, graph, claims,
references, deferred-index, and media-job stores. The exclusion registry is
versioned and covered by golden tests so a new derived sidecar cannot silently
enter exports.

Restore preparation never overlays a live vault. It validates archive version,
hashes, sizes, duplicate paths, path traversal, absolute paths, symlinks, hard
links, and device entries; extracts into a new empty staging root; verifies the
required scaffold; and atomically promotes the staged root only after all checks
pass. Derived indexes start absent and rebuild from canonical files. Readiness may
serve lexical recall while optional semantic/media indexes warm.

User export and provider backup use the same canonical manifest contract. Export
is a portable archive; encryption-at-rest, object-store retention, and KMS key
handling around provider backups are Substrate/deployment responsibilities.

Account deletion is orchestrated, not a command exposed to the model-facing cell:
Substrate suspends routing and revokes sessions/transfers, stops the cell,
optionally obtains the policy-required final snapshot, destroys the tenant key
and storage, removes the registry binding, and records a content-free deletion
audit event. Exomem supplies stop/quiesce/snapshot hooks and reports when no
writer remains.

### 8. Privacy-preserving observability is the hosted default

Hosted operational logs contain only timestamp, opaque cell ID, request ID,
command name, success/error code, duration, byte/count buckets where safe, release,
and resource/worker health. They do not contain query text, command arguments,
vault-relative paths, titles, excerpts, OAuth tokens, transfer grants, cell
credentials, account email, or Paddle identifiers.

The existing local relevance-feedback JSONL logs are disabled in hosted cells.
Usage-aware ranking that depends on those logs remains off unless a future
tenant-local, consented design restores it without provider-visible content. Log
roots are unique per cell even when logs are shipped centrally, and central
labels use opaque IDs only.

Security events such as rejected service credentials, protocol mismatch, token
replay, path escape, archive rejection, and cross-cell credential use are logged
without echoing the presented secret or requested content.

### 9. Entitlements configure cells but never become canonical knowledge

Substrate is the source of truth for billing events and internal product
entitlements. Before routing, it denies suspended or unentitled operations. At
provision/start time it also supplies a bounded cell policy covering retrieval
mode, storage/upload limits, and optional worker limits. The cell enforces that
policy locally as defense in depth; a client cannot enable premium compute by
passing `mode="hybrid"` or media flags.

An unavailable or unentitled optional subsystem degrades to durable capture and
lexical/BM25 recall. It never makes the vault unavailable and never invokes a
reasoning model. Changes in Paddle state reach the cell only through Substrate's
internal entitlement projection; cells never call Paddle in the request path.

## Risks / Trade-offs

- **One process/container per tenant has higher idle memory and scheduler cost.**
  → Start with lean cells, lazy optional workers, hard resource limits, shared
  read-only model caches, and measured scale triggers. Reconsider pooling only
  after load data and a dedicated same-process isolation design exist.
- **Serial mutation locking reduces write throughput.** → Personal vault writes
  are low-volume and correctness dominates. Keep expensive extraction outside
  the commit guard, bound lock wait, and instrument queue duration without
  content.
- **A long or wedged mutation can delay snapshot and later writes.** → Use bounded
  operations, lock timeouts, draining readiness, worker cancellation points, and
  operator-visible `MUTATION_BUSY`/stuck-writer health.
- **A lock added only to request dispatch misses background writers.** → Require
  a process-safe lock and conformance-test every mutation entry point; media stays
  default-off until its child process participates.
- **Process isolation can still fail through bad mounts or a shared `.env`.** →
  Hosted startup skips dotenv, validates resolved roots and ownership, and fails
  readiness before serving. Deployment tests inspect container mounts and ensure
  no cell can see another tenant directory.
- **The shared gateway is a high-value confused-deputy target.** → Derive routing
  only from verified server-side identity, expose no tenant selector, use unique
  per-cell credentials, private networking, short-lived transfer grants, and
  adversarial routing tests.
- **A control-plane compromise can misroute an authenticated request.** → Physical
  cell mounts and unique cell credentials limit blast radius, but cannot eliminate
  it. Protect registry changes with audit, least privilege, key rotation, and
  eventually mTLS/workload identity.
- **Snapshot exclusions may omit a newly introduced durable file or include a new
  derived secret/state file.** → Version an explicit classification registry,
  require new sidecars to declare durability, and test round-trip restore plus a
  denylist of secrets/transients.
- **Derived indexes are absent after restore, reducing recall temporarily.** →
  Serve lexical recall immediately, report warming state honestly, and rebuild
  derived stores asynchronously from canonical Markdown/media.
- **Server operators can access plaintext while Exomem searches.** → Make the
  trust ceiling explicit: encryption at rest and owner-scoped access, not
  zero-knowledge. Keep query/content out of operational logs and minimize human
  access to cell volumes.
- **Gateway and cell releases can drift.** → Version the protocol, pin cell
  releases per tenant, maintain backward-compatible additive changes, and run
  gateway/cell contract tests before rollout.
- **Private bearer credentials are weaker than workload identity.** → Use unique
  rotatable credentials over a private network for the alpha; leave the protocol
  compatible with mTLS or platform workload identity later.

## Migration Plan

1. **Land behavior-preserving core safety.** Add the process-safe mutation
   coordinator behind all existing command surfaces, route uploads through it,
   add concurrency tests, and keep ordinary local defaults unchanged. Add query
   redaction controls before any hosted data exists.
2. **Add hosted-cell mode and portability tooling.** Implement fail-closed config
   validation, service authentication, private readiness, feature policy,
   snapshot/export, restore preparation, and their adversarial tests. Exercise a
   cell locally with isolated vault/state/log roots and media workers off.
3. **Publish the versioned contract.** Freeze command-catalog, forwarding,
   transfer, readiness, idempotency, and snapshot manifest fixtures for the
   companion Substrate change.
4. **Integrate the Substrate sandbox.** Substrate implements public auth,
   identity-to-cell routing, invite UI, friendly capture, Paddle sandbox/internal
   entitlements, provisioning, and encrypted backup storage. Run end-to-end tests
   against two or more cells before inviting users.
5. **Run lifecycle drills.** Prove quiesced backup, portable user export, restore
   with every derived sidecar removed, release rollback, suspension, credential
   rotation, and destructive deletion in a non-production tenant.
6. **Invite-only alpha.** Start with lean lexical cells and complimentary or
   sandbox entitlements. Monitor content-free latency/error/resource metrics and
   verify the invite-to-first-useful-recall journey without manual server work.
7. **Enable paid and optional compute gradually.** Move Paddle live only after
   webhook reconciliation and entitlement tests pass. Enable embeddings and then
   media per tier only after resource limits and process-safe worker commits are
   proven.

Rollback never rewrites canonical tenant data. The control plane stops new
routing, drains or stops the affected cell, restores the previous pinned cell
image/protocol-compatible gateway, and restarts against the same vault. Before a
release that changes canonical file format, Substrate takes a verified snapshot;
derived sidecars may always be discarded and rebuilt. A failed hosted rollout can
be disabled while local/self-hosted Exomem continues unchanged.

## Open Questions

- Which infrastructure supplies production workload identity after the alpha:
  private per-cell bearer credentials, mTLS, or platform-native identities? The
  cell contract must support rotation without exposing the choice publicly.
- What measured cell count, memory pressure, or cold-start target would justify a
  future pooled runtime? There is intentionally no speculative pooling design in
  this change.
- Should portable exports use ZIP with selective compression or a tar-based
  archive? The manifest and security semantics are format-independent, but the
  first implementation should favor broad user portability and streaming large
  media.
- What retention windows apply to normal backups, deletion grace, and requested
  user exports? Substrate must decide and disclose these before live launch.
- Which KMS/object-store combination and regional placement meet the first live
  deployment's cost and privacy requirements? This is a deployment/Substrate
  decision, not a cell storage-format decision.
