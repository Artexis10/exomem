## Context

`seamless-managed-worker-handoff` (archived 2026-09-14) made a promoted worker carry the standby's readiness and seed the adopted recall origin. Two paths were left outside the carry. The watcher's `finish_startup_recovery` runs `_validate_existing_graph_on_seed` on every worker, promoted or cold, and its admission check `_await_admissible_graph` requires `durable_checkpoint_is_coherent()` and `available()` within a fifteen-second budget that only extends while the boundary is held. The incremental drain refuses to run under any external mark (`drain_paths` returns `requires_rebuild` when recall is not live), and the only code that clears a mark is the watcher's withdrawal path.

## Decisions

- D1: The carried set gains `startup_validation`, set at promotion when and only when `graph_handoff` was carried on a `current` verdict. `finish_startup_recovery` consults it and takes the O(1) durable check alone; the source-bytes proof is the one the standby ran. A cold start carries nothing and validates in full, unchanged.
- D2: `_await_admissible_graph` distinguishes three answers: admissible; incoherent with the boundary free (the genuine case, proceed to suspend and rebuild); and unacknowledged with the boundary held by a governed mutation. The third answer waits for that holder's release and re-checks once; a write's own acknowledgement is what makes the checkpoint coherent, so the wait is bounded by the write, not by a constant. If the re-check still fails with the boundary free, that is incoherence.
- D3: `drain_paths` accepts a path-scoped mark whose paths are a subset of the batch it is about to re-derive: it re-derives them, and on publication retires the mark for exactly those paths through the same compare-and-swap the watcher's withdrawal uses, inside the drain's own hold. An unscoped mark keeps refusing the drain, as today, because the affected set is unknown.

## Risks

- D1 assumes the standby's proof covers the bytes the promoted worker serves. It does: the standby proved the checkpoint, promotion compared checkpoint pairs (or re-proved after a migration), and the residue was applied under the lease. A write that lands between promotion and the watcher's boot pass acknowledges its own checkpoint.
- D2 can wait for a long write. That is the correct cost: the alternative is an 85-second rebuild with reads suspended, paid on the write's account.
- D3 must not retire a mark for a path whose event the watcher has not yet observed. Retirement is scoped to the paths the drain re-derived from canonical bytes in the same hold, so a newer event on the same path lands a new mark afterwards.

## Measurements to record at closure

Promotion to admitted graph on the live vault with a write in flight during start-up validation; product E2E restart step ten runs without a `recovery_required` timeout.
