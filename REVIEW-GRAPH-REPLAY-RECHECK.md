# Recheck: PR #1431 `fix/graph-replay-currency` (review-correction round)

**Verdict: REQUEST_CHANGES.** All five original findings are fixed. One new concern
(hypothesis a) is confirmed: the replay's drain can still run a whole-vault pass on
the caller's thread, including for mutation-request callers that can report `pending`.

Reviewed `53ac15a2..544662f`, focusing on the correction commits.

## Original findings

| # | Status | Evidence |
|---|--------|----------|
| F1 [HIGH] | **FIXED** | The replay no longer calls `drain_paths` directly. It queues `stale ∪ delta` and calls `index_sync.drain_graph_work(paths=scope)` once the hold is released (`epistemic_graph.py:5227-5247`). That call picks up the marker branch, the epoch gate and CAS clearing (`index_sync.py:1052-1109`). The original repro now ends with marker=None, available=True and one pass (`converge_full_graph_marker`), so nothing is published while the marker stands. The three rule tests fail at `58e8d05` and pass at the head. |
| F2 | **FIXED** | `test_the_oracle_sees_a_replay_drain_that_does_not_widen` stubs `_topology_affected_sources` to return nothing and asserts that the oracle raises. It passes, so the retitle fixture (each title linked from its own page) can now detect missing widening. |
| F3 | **FIXED** | `_replayed_path_currency` re-spells each path as `vault_root / rel` before the recall-policy, registry and corpus checks. The alias test fails at `d1879d9` and passes at the head. |
| F4 | **FIXED** | Created paths now join `outside` (deduplicated) and are filtered to `delta_paths` once proved current, so the later "created without a delta row" check no longer fires. The test fails at `d1879d9` and passes at the head. |
| F5 | **FIXED** | `_assert_matches_a_fresh_rebuild` now also compares `graph_nodes (kind, path, title)` and `graph_dependencies (source_path, lookup_key, raw_target)`. |

## NEW CONCERN 1 (hypothesis a): the whole-vault pass still runs on the caller's thread

**Where:** `src/exomem/epistemic_graph.py:5242-5247`, and `index_sync.py:1067`.

**Scenario:** this is the common live state after a deferral: a standing full marker, or
an epoch that isn't settled. The replay path runs a whole-vault pass synchronously in
two ways:
1. With a marker standing, `drain_graph_work(paths=)` enters its marker branch and runs
   `converge_full_graph_marker`. That call is `rebuild_all` inline.
2. With an unsettled epoch, the drain returns 0. The scope stays queued, so the code sets
   `_rebuild_after_release` and runs `_rebuild_all_off_boundary` inline.

Only a parent handoff is exempt, not a mutation-request caller, even though
`_caller_can_carry_pending` is true for it.

**Reproduction.** I wrapped `converge_full_graph_marker` and `_rebuild_*` with a thread
spy and set `writer_lease._ACTIVE_MUTATION_TRACE`:

```
REQ-MARKER calls [converge_full_marker, rebuild_all, rebuild_all_off_boundary] on MainThread
REQ-EPOCH  calls [rebuild_all_off_boundary] on MainThread; receipt still queued (1)
```

Both runs report `code='incremental_completed'`.

**Impact.** Main also rebuilt here, so this is not a regression. It does keep the
59-101 s rebuild on the write path in exactly the states where replays cluster. In the
epoch case the receipt also stays queued, so the daemon drains it a second time.

**Minimal fix.** The receipts are already durable, so a caller that can carry `pending`
should hand the work to the daemon:

```python
if _caller_can_carry_pending(self.vault_root, self._mutation_coordinator) and (
    deferred_index.graph_full_rebuild_pending(self.vault_root) is not None
    or not self.epoch_admits_incremental_repair()
):
    return {"indexed_files": 0, "nodes": 0, "edges": 0, "deferred": 1, "queued": 1}
```

Put this just before `drain_graph_work(paths=scope)`. Prototyped: both request repros
make zero whole-vault calls, standalone callers keep their contracted join, and 26 tests
pass. Add a red-first test under a request trace.

## Hypothesis b: deadlock — not found

The coordinator hold is RLock-backed and re-entrant (`mutation_lock.py:1763,2000`), and
the replay drains after releasing its own hold. The nested route (`drain_deferred_work` →
`upsert_after_write` → `drain_graph_work`) re-enters only that lock. `_derived_fanout_lock`
is a leaf lock around a memo dict.

## Hypothesis c: ownership — not violated

`paths=` only narrows `snapshot_graph`, so only **queued** receipts drain, and CAS keeps
newer revisions. Other writers' delta pages in the scope were queued by this pass first.

## Minor

- `6a3a21c` is titled `wip: checkpoint in-progress correction round`. That breaks the
  conventional-prefix rule. Squash it or reword it.
- `incremental_completed` on a path that ran a whole-vault pass hides the latency.
- OpenSpec 9.4 is accurate; add the concern 1 deferral once it is addressed.

## Verification run

- `tests/test_graph_replay_currency.py`: 11 passed at the head. At `58e8d05`, 3 failed
  (the F1 rules). At `d1879d9`, 2 failed (F3, F4).
- The deferred-queue, drain, index_sync and replay suites: 124 passed.
- `uvx ruff check --select F src tests`: clean.
