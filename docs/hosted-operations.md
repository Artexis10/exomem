# Hosted cell operations

This is the operator contract for Exomem's managed hosted profile. It does not
replace the personal local or remote profiles: ordinary MCP, REST, CLI, and
single-user deployment remain unchanged.

The unit of isolation is one single-vault Exomem cell. Substrate is the shared
control plane and public product. The detailed handoff is in
[substrate-control-plane-contract.md](substrate-control-plane-contract.md).

## Hard invariants

- One opaque, immutable cell ID maps to one private process/container, vault
  mount, state root, log root, and credential set.
- Cells may share immutable application images and model-weight caches. They do
  not share writable vaults, sidecars, locks, idempotency records, logs, temp
  roots, OAuth state, or secrets.
- The public gateway derives the destination from authenticated server-side
  state. A public tenant/cell/path selector never influences routing.
- Every canonical mutation, transfer commit, snapshot, and restore publication
  participates in the same process-safe vault mutation boundary.
- A cell receives provider-neutral capabilities and resource bounds. It does not
  receive Paddle products, prices, subscriptions, or credentials.
- A failed or unavailable cell fails only its mapped tenant. The gateway never
  retries against another tenant's cell.

## Startup and readiness

Hosted mode is explicit and fails closed. Trusted operator configuration must
provide the opaque cell identity, absolute non-overlapping vault/state/log roots,
private gateway trust, protocol version, release, and resource policy. Hosted
startup does not load a working-directory or vault `.env`.

Before serving traffic, verify:

1. the process can see only its assigned mounts and none are unexpected
   symlinks or shared writable roots;
2. the vault scaffold and immutable cell binding are valid;
3. private service authentication and the mutation coordinator are healthy;
4. raw query/relevance/access logs are disabled and the log root is cell-local;
5. enabled optional workers have both entitlement and resource/mutation-safety
   clearance; and
6. the gateway/cell protocol versions are compatible.

Liveness means the process answers. Readiness means it can safely serve the
reported read/write class. A content-free readiness response may include check
names, stable error codes, opaque cell ID, release/protocol, lifecycle state,
mutation health, and derived-worker ready/warming/degraded state. It must not
include account identity, vault paths, filenames, content counts, queries,
secrets, or billing identifiers.

Optional embeddings or media may be warming or degraded while canonical capture
and lexical recall remain ready. Do not mark the whole cell unavailable merely
because optional derived state is absent.

## Lifecycle runbook

| State | Gateway behavior | Cell/operator action |
| --- | --- | --- |
| provisioning | no tenant routing | initialize an empty staged root, validate, then atomically publish |
| ready | route compatible entitled traffic | serve through the private registry/transfer boundary |
| quiescing | stop new mutations; retry safely | drain in-flight mutations and stop durable background writers |
| quiesced | no ordinary writes | snapshot, replace, suspend, or hand off to another lifecycle operation |
| restore-staging | no routing to the candidate | verify the complete archive into a new root; never overlay live data |
| deletion-sealed | reject reads, writes, transfers, and readiness | return the idempotent cell checkpoint; await control-plane destruction |

Provisioning is retryable and non-destructive. It may initialize an empty root
from the generic scaffold, but it must not overwrite an existing or incompatible
vault to force readiness.

Quiesce before snapshot, restore publication, release replacement, or deletion.
Stop new admission first, bound the drain, and report success only after no
request or background writer can change canonical bytes. A failed drain returns
a stable retryable lifecycle error; it is not permission to proceed unlocked.

## Protocol, rollout, and rollback

The gateway declares a supported contract version; the cell accepts it or
returns a content-free incompatibility error. Command names, schemas, read/write
metadata, result envelopes, and stable error codes come from Exomem's command
registry and conformance fixtures, not a hand-copied gateway catalog.

Within a compatible protocol, releases may add optional response fields. Removing
or changing a command, parameter, retry rule, transfer rule, or stable error is a
breaking contract change and requires a coordinated gateway/cell rollout.

Pin the cell image/release per tenant. Roll out a tested gateway/cell pair to a
canary cell, verify private readiness and content-free error behavior, then widen
the cohort. Before any canonical file-format migration, take and verify a
portable snapshot.

Rollback is tenant-sized:

1. stop routing to the affected cell and quiesce it;
2. choose the previous image that is compatible with the active gateway (or roll
   the gateway contract back as the coordinated pair);
3. start that image against the same canonical vault and isolated runtime roots;
4. pass full readiness before restoring routing; and
5. discard/rebuild derived sidecars as needed.

Rollback never rewrites canonical Markdown, media, schema, history, or review
state. If the old release cannot read the canonical format, restore from the
verified pre-migration snapshot into a new staged root; do not improvise an
in-place downgrade.

## Export, backup, and restore

Exomem produces a deterministic, manifest-verified canonical archive and an
opaque private artifact reference. It excludes credentials, operational logs,
locks, temporary files, hosted binding/idempotency state, voice profiles/models,
generated frames, and rebuildable SQLite/WAL/SHM sidecars.

Substrate decides whether the operation is a user export or provider backup and
owns encrypted object storage, KMS keys, retention, delivery authorization, and
expiry. Restore validates paths, entry types, duplicates/case collisions,
resource bounds, sizes, and digests before extracting to a new staging root.
Publish atomically; then rebuild optional indexes from canonical bytes. Lexical
recall may become ready while those indexes warm.

After Substrate durably copies and verifies an archive, it explicitly releases
the export checkpoint. The cell persists that acknowledgement first, removes
the digest-addressed local ZIP, fsyncs the export directory, and only then
resumes. Replaying a lost acknowledgement repeats the cleanup safely; a cleanup
failure leaves the checkpoint replayable and the cell quiesced.

## Encryption and observability ceiling

The hosted service can provide encrypted tenant volumes, encrypted backups,
owner-scoped access, private networking, and tightly controlled secrets. It is
not zero-knowledge or end-to-end encrypted compute: Exomem must hold plaintext in
cell memory to parse, search, and index tenant material, and a sufficiently
privileged infrastructure operator can access a running cell or its volume.

Minimize that trust surface. Human volume access should be exceptional,
time-bounded, and audited. Central logs and traces contain opaque cell/request
IDs, operation name, stable result/error, duration, release, and safe resource
buckets only—never bodies, queries, titles, paths, filenames, arguments,
credentials, transfer grants, account email, or Paddle identifiers.

## Billing is outside the cell

Paddle checkout, webhooks, customer portal, catalog, subscription reconciliation,
and complimentary alpha access belong to Substrate. The control plane converts
that state into an internal provider-neutral entitlement revision before routing
and at provisioning/startup. The cell enforces the supplied capability and
resource bounds as defense in depth and never calls Paddle in a request path.

Paddle MCP can help an operator configure or inspect sandbox/live billing state.
It is not installed in Exomem cells, used by the gateway/cell protocol, or
required for local/self-hosted Exomem.
