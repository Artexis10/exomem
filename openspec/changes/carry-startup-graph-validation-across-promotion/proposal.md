## Why

A promoted worker can inherit a proved graph and still start a whole-vault rebuild.
An adopted residue deliberately withdraws graph availability while its durable receipts wait for the drain.
Startup currently treats that withdrawal as failed validation, before activation starts the drain.
A busy drain can also lose its repair ownership because an earlier blocked result overrides its newer contention result.

A matching sidecar checkpoint does not prove canonical bytes across the standby's unwatched interval.
Likewise, a path receipt and matching file metadata do not prove that every reader sees the repaired bytes.
The change must carry source proof through observation, keep repair pending until publication, and preserve genuine cold recovery.

## What Changes

- Carry startup validation only after the normal watcher observes changes and a bounded replay seals the standby's source proof. Keep a proved residue fenced until its durable repair completes.
- Distinguish admissible, pending and stably incoherent startup state under the actual mutation boundary. A busy writer does not justify suspension or rebuild.
- Re-prove a snapshot that the serving worker published after the standby's proof inside the promotion request, from the page reads the standby's proof ended on, so the graph handoff is still carried and writes are admitted at promotion. On 2026-11-02 a rehearsal promoted as `advanced` after such a publication, and the promoted worker refused writes as warming for the 100-second graph handoff.
- Let the drain publish exact observed bytes, repair bounded batches, and retire only the path epochs it proved after commit. Carry partial checkpoint coverage durably through restart, and preserve recovery for unknown or incompatible state.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `managed-service-upgrades`: start-up validation counts as carried; contention during validation is not incoherence; promotion re-proves a moved snapshot from the standby's reads.
- `instant-start`: the drain retires path-scoped marks it re-derives.

## Impact

`epistemic_graph.py` (the seeded, deadline-bound cold proof), `vault.py` and `recall_policy.py` (one walk for both memberships), `service_manager.py` and `server_assets.py` (the promotion request budget), `file_watcher.py` (`finish_startup_recovery`, `_validate_existing_graph_on_seed`, `_await_admissible_graph`), `warmup.py` and `service_standby.py` (the carried set and its completion record), `graph_drain.py`, `index_sync.py` and `freshness.py` (mark retirement inside the drain), `tests/test_standby_handoff_writes.py`, `tests/test_graph_deferred_queue.py`, the product E2E restart step, `docs/managed-service-upgrades.md`. The promotion request body gains an optional `request_seconds` field; a standby that receives none assumes the legacy ten-second cap. No change to the transport or write scope.

The remaining work also touches the existing semantic, resolver and inbound publication owners. No new queue, endpoint or external dependency is proposed.
