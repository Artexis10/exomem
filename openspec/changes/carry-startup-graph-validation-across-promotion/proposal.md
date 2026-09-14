## Why

The standby sequence shipped in 0.85.1 admits writes at promotion and serves the first write incrementally, but two proofs still repeat or stall after a worker replacement. Measured on the personal service on 2026-09-14 (0.85.0 to 0.85.1, 4,281 pages): the promoted worker's watcher re-ran the start-up source-bytes proof its standby had already completed and promotion had re-verified; that proof finished while the first governed write was mid-commit, so the durable checkpoint read as unacknowledged, the boundary stayed held past the fifteen-second admission budget, and validation suspended reads and rebuilt the whole vault for 85 seconds before the graph drain started. Separately, the product E2E's restart step fails intermittently on main because a path-scoped external mark left standing across a restart starves the drain: every pass reports the vault not ready for incremental repair, barrier repair declines on the pending change, and nothing retires the mark inside the 120-second window except the watcher's 300-second reconcile.

## What Changes

- Carry the watcher's start-up graph validation across promotion: when the graph handoff was carried on a current verdict, the promoted worker admits the graph on the durable checkpoint alone and does not repeat the source-bytes proof.
- Treat an unacknowledged checkpoint under a held boundary as contention, not incoherence: start-up validation waits for the in-flight governed write's own acknowledgement instead of a fixed budget, and never suspends reads or rebuilds on that evidence alone.
- Let the incremental drain retire a path-scoped external mark for exactly the paths it re-derives, inside its own boundary hold, so convergence after a restart happens at the drain's cadence.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `managed-service-upgrades`: start-up validation counts as carried; contention during validation is not incoherence.
- `instant-start`: the drain retires path-scoped marks it re-derives.

## Impact

`file_watcher.py` (`finish_startup_recovery`, `_validate_existing_graph_on_seed`, `_await_admissible_graph`), `warmup.py` and `service_standby.py` (the carried set and its completion record), `graph_drain.py`, `index_sync.py` and `freshness.py` (mark retirement inside the drain), `tests/test_standby_handoff_writes.py`, `tests/test_graph_deferred_queue.py`, the product E2E restart step, `docs/managed-service-upgrades.md`. No change to the transport, the supervisor protocol, or write scope.
