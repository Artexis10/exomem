# Proposal: bound-contended-write-index-refresh

## Why

During the 2026-08-26 0.63.0 acceptance, a 356-file semantic import burst on
the personal cell chained follow-on whole-vault builds with retrieval-warming
windows (one live client request stalled 25 minutes). Tracing the recording
site (`vault.py` "index upsert incomplete after batch_atomic_write; durable
full-index refresh recorded") against the code established the real trigger:

**A batch write during an embedding warm-up window is double-accounted.** The
embeddings component defers with `("embeddings", "deferred",
"deferred_warmup")` after ALREADY recording durable, path-exact semantic
receipts for the batch (`embeddings.py` warm-up path). But
`index_sync.full_upsert_succeeded` carves out only the code
`"deferred_durable"`, so the warm-up deferral fails the batch report and
`record_failed_refresh` mints a durable FULL-component receipt for paths whose
semantic replay is already durably queued. Draining each full receipt runs
`recover_full_receipt_graph_epoch(build=True)` — a whole-vault graph rebuild —
and re-dispatches the entire component fan-out. Three contended-era batch
writes during the burst window produced exactly the three measured demands,
the chained builds, and hours of graph `recovery_required` churn.

O(vault) response to an O(1) cause, at the accounting layer: the deferral was
already bounded and durable; declaring it failed is what escalates.

## What Changes

- `full_upsert_succeeded` accepts an embeddings `deferred_warmup` outcome as
  success **when and only when** the deferral durably covers the batch's
  semantic paths (the warm-up path's receipts), so no full-component refresh
  demand is minted for work that is already durably queued path-by-path.
- A warm-up deferral that did NOT record covering receipts still fails the
  report and mints the full-component demand — fail closed, and the
  escalation is counted in stable content-free telemetry alongside a counter
  for the accepted-covered case.

## Related debt discovered by the same trace (explicitly OUT of scope here)

A lexical batch upsert refused by the publication barrier's 50 ms foreground
timeout is swallowed at module-level `lexstore.upsert_after_write` (the
`upsert_paths` return value is discarded; same for deletes): the batch report
never learns of it, and the only recovery is the process-local, non-durable
`_DEFERRED_UPSERTS` registry until the periodic reconcile heals it (durably,
post-0.63.0). Making that refusal observable changes
`post_commit_batch_fanout` semantics consumed by media completion and client
degradation envelopes — a deliberate follow-up change, not a rider here. This
change pins the current swallow behavior in a test so the debt is measured,
not silent. A mixed batch — recall-candidate and non-candidate markdown in
one write — still fails the coverage check and mints the full-component
receipt, exactly as the pre-existing `deferred_durable` carve-out always has:
that is spec-conformant fail-closed residual, and live acceptance (task 3.3)
must not read those receipts as this fix failing.

## Capabilities

### Modified Capabilities

- `live-index-freshness` — a component deferral that is already durably
  path-covered SHALL NOT fail the batch report nor seed a full-component
  refresh demand; whole-vault work requires a component whose incomplete work
  has no durable path coverage.

## Impact

- `src/exomem/index_sync.py` — `full_upsert_succeeded`'s deferral carve-out;
  telemetry counters.
- Regression tests reproducing the warm-up double-accounting red-first, plus
  the lexical-swallow baseline pin.
