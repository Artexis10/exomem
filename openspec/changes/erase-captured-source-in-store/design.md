## Context

`erase-captured-source` retires one captured Source incarnation in file-mode vaults.
Its eraser closes the canonical page, the catalogue copies, attempt allocations, derived rows and reusable caches, and reports `complete` only when each owner acknowledges.
A store-mode vault refuses erasure with `SOURCE_ERASURE_MODE_UNSUPPORTED`, and its policy read reports erasure disabled.
That change's design, especially D2, D3 and D7, stays the contract here; this change extends it to the store and closes the remaining evidence gaps.

## Goals / Non-Goals

Extend complete retirement to store-mode vaults through the canonical store owner.
Close the evidence gaps that keep some Sources `unknown`: legacy coverage, residue producers and no-effect dependency clearing.
Physical erasure of SQLite free pages and WAL storage stays outside the guarantee, as filesystem free blocks do.

## Decisions

### D1. Preserve store sequencing while retiring identifying material

The store owner retains existing store, projection, rollback and replica-publication modules.
This change owns the new `vault_store/purge.py` after the merged projector baseline.
Its only existing store-schema edit narrows the audit trigger through one migration, reviewed by the schema owner.
The integration uses existing guarded store commits: compare the target revision and hash inside the delete transaction.
Store-wide monotonic revisions distinguish a replacement page even when its path or bytes match an earlier incarnation.
Resolve only held rows proved to belong to the target; an unrelated person's edit is not erasure authority.

After target-qualified projection and vector cleanup finish, redact identifying audit fields rather than deleting audit rows.
Replace `path`, and `from_path` when set, with one opaque token per target life and original path.
Never reuse a token across lives or targets. Both move halves use the same token for the same original path.
Require `reserved_paths.classify_logical` to block each token and `ownership.classify` to classify it as something other than a page.
Keep a null `from_path` null: audit readers use that distinction to identify move halves.
Never make `path` null: vector readers interpret that value as a vault-wide reset.
Set `sha256` null and replace the unstructured `reason` with fixed redaction text.
Retain `seq`, `revision`, `operation`, the closed origin token and timestamp so existing cursors keep their order.
Follow exact move lineage for the target's life; do not redact a replacement incarnation's later life at a reused path.
The schema migration permits only this qualified redaction update, with sequencing fields unchanged, null move origins preserved and `sha256` cleared.
It refuses all other audit updates and every audit deletion.

Managed reusable store snapshots and published replicas must retire or replace versions older than the purge revision.
The component receipt binds the physical store and purge revision, including held content, projection and replica publication acknowledgements.
Collection transaction sequence alone cannot prove freshness because page commits advance the store revision separately.
An active overlapping reader may finish; a managed snapshot must not later republish the retired data.

The guarantee excludes forensic remnants in SQLite free pages and WAL storage, as it excludes filesystem free blocks.
Do not add `secure_delete`, checkpoint or vacuum work to claim physical erasure that this contract does not promise.
The store owner has agreed this contract; implementation still requires the merged projector baseline.
Run the projector, migration and served-store suites before delivery because each reads this audit trail.

### D2. Restore a latch only through its producer's proof

A dependency reservation that proves no effect or complete rollback restores the latch state from before it.
Today `reserve_publication` marks legacy uncertainty on every reservation, so no reservation ever clears.
Mark legacy uncertainty only where version-one evidence was actually lost.
Call bounded dependency reconciliation from the producers' recovery paths, so another replica can resolve an incarnation from canonical custody.

### D3. Establish legacy coverage by a bounded producer census

Sources enrolled before a producer birth stay `unknown` until coverage is proved.
A bounded, caller-driven producer census establishes their coverage from structured references and exact ownership evidence.
The existing watcher ingress invalidates coverage for an unexplained external change. The census never scans all history or reads prose.

### D4. Qualify latency on the generated reference

The file-mode change proved that a progress call's statement and step counts stay constant as copies and unrelated Sources grow.
This change runs the generated 20,000-Source reference against the public warm-call budget: p95 below one second for one progress call.
Record terminal failures, pending outcomes, component work bounds, query count, lock contention and resources with the maintained instrument.

## Risks / Trade-offs

- The audit migration narrows a trigger that projector and served-store readers depend on; their suites run before delivery.
- A managed replica that misses the purge revision keeps retirement pending; replica loss can lose custody evidence.

## Migration Plan

1. Ship the store purge with store-mode erasure still refused; verify ordinary store capture and deletion.
2. Prove purge, redaction and replica retirement in an isolated store cell.
3. Lift the store-mode refusal; enable a store cell only under its existing operational authority.
