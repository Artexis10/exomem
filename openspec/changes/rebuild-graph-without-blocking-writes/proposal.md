## Why

A full epistemic-graph rebuild must not hold canonical writer authority for the
duration of a vault-sized build. The first implementation attempted to solve
that with an owned `graph_pending` idempotency state. That conflates the
canonical mutation's durable outcome with a derived graph flight: its owner can
die, its graph result can become stale after another canonical commit, and it
does not give every unacknowledged checkpoint a durable recovery path.

The protocol must make canonical truth first-class, acknowledge the unavoidable
multi-store crash cut honestly, and let graph health be recomputed or joined
after canonical commit without ever re-running the leaf.

## What Changes

- Replace `graph_pending` with non-owned canonical lifecycle states
  `reserved`, `executing`, `canonically_committed`, and `completed`. A lease is
  scoped to one execution attempt; it is not embedded as durable graph
  ownership.
- Bind every successful claim to one hidden, portable, content-free
  `GraphCommitReceipt` v2. It binds the namespaced idempotency-key digest,
  command/payload digest, per-claim lowercase-24-hex commit token and attempt,
  a closed path/content-free canonical terminal projection, and exact checkpoint
  generation/digest plus HMAC-covered `canonical_disposition`
  (`success|committed_failure`). It is
  atomically installed once and HMAC-SHA256 authenticated by a fresh private
  32-byte per-attempt secret retained only in owner-only local idempotency
  runtime state (owner-only runtime directory/database permissions); synced
  vault artifacts are advisory, never proof. The v2 HMAC input is a versioned
  domain plus canonical closed JSON of every receipt field except its HMAC;
  malformed/duplicate/surplus fields fail before constant-time verification.
  Windows uses a bounded local SQLite binary DPAPI `CurrentUser` BLOB with
  attempt/token-bound entropy and a protected service DACL; malformed,
  copied/profile-lost, or raw-legacy secret material fails closed as
  `outcome_unknown`.
- Make `floor → caller files → checkpoint → one authenticated receipt` the
  ordinary protocol. There is no mutable `commit_point` and no two-write
  marker. Receipt signing follows only a successful guarded vault batch and
  bounded route-selection fanout preconditions; it excludes later local CAS and
  graph work. A crash after canonical files but before an authenticated receipt
  is the fail-closed `outcome_unknown` terminal classification, never a
  replayable write.
- Keep the public `.graph-sync.json` v1 bytes and digest vectors unchanged;
  retain the internal generation floor and make floor/checkpoint/ack admission
  and migration closed and strict.
- Treat graph health as dynamically joinable and recomputable from the exact
  committed receipt/checkpoint. An acknowledged checkpoint, an exact registered
  rebuild handle, or an explicit durable failure handle is required before the
  operation guard releases; there is no accepted-but-unverified handoff.
- Keep private full builds outside canonical authority, but serialize exact,
  revalidated live publication under the canonical boundary. The private phase
  commits exact acknowledgement/checkpoint metadata, truncates WAL, runs full
  integrity and direct source/membership/topology proof, and emits an immutable
  ticket binding exact graph publication epoch, direct projection identity,
  warmed live `RecallFreshnessCheckpoint`, recall-policy version plus bounded
  no-follow `_access.yaml` snapshot/fingerprint, no-external-pending epoch,
  and exact closed temp-file/meta identity. Under authority, publication
  performs only two O(1)/bounded ticket checks around the publication hook,
  then atomic replacement. POSIX uses a
  cooperative lock directly below an owner-controlled runtime root; Windows
  retains its stronger no-delete-share replacement refusal.
- Make delete, recursive delete, restore, recovery, and rollback obey the same
  epoch and receipt rules. Setup/recovery/rollback work is never a mutation
  commit point.
- Defer cross-replica exactly-once: without a shared pre-leaf CAS, a copied
  receipt cannot suppress a mutation. Recovery never scans receipt artifacts;
  it reads only the exact artifact named by a matching local attempt record.
- Observed external Markdown or access-policy edits mark the epoch pending and
  fail closed; missed or unobserved direct edits are eventually caught by
  watcher-periodic reconcile. Arbitrary-editor linearizability requires a
  broker/journal and is out of scope.

## Capabilities

### New Capabilities

- `graph-rebuild-availability`: durable canonical receipts, exact graph work
  handles, off-boundary single-flight rebuilding, strict epoch recovery, and
  non-partial publication.

### Modified Capabilities

- `hosted-mutation-safety`: private graph construction and narrowly scoped temp
  cleanup may occur under the rebuild lock; every canonical or reader-visible
  publication remains serialized by the canonical mutation boundary.

## Impact

Affected areas are canonical batch publication, mutation/idempotency state,
graph coordination and acknowledgements, deletion/recovery lifecycle, runtime
readiness/reconcile, and installed-product verification. User-authored Markdown
schema, MCP selector names, OAuth, and graph query shapes do not change. The
graph SQLite metadata and hidden operational artifacts remain content-free and
excluded from semantic indexing.
