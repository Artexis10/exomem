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

## Recheck 2 (head `99aae8b`)

Reviewed `544662f..99aae8b`: `epistemic_graph.py`, `tests/test_graph_replay_currency.py`
and task 9.4.

| Item | Status | Evidence |
|------|--------|----------|
| NEW CONCERN 1 | **FIXED** | Before the drain (`epistemic_graph.py:5237-5250`), a caller that can carry `pending` returns `deferred`+`queued` when a marker stands or the epoch refuses. |
| Label change | **PARTIAL** | Correct when there is no checkpoint. When the acknowledgement covers the checkpoint, the new deferral is reported as `graph_rebuild_completed` (see NEW CONCERN 2). |
| Task 9.4 | **Accurate** | Its claims hold, but it doesn't cover concern 2. The three new tests fail with `544662f`'s `epistemic_graph.py` and pass at the head. |

**Thread-spy repros** (spies on `converge_full_graph_marker`, `rebuild_all` and
`_rebuild_all_off_boundary`; a mutation-request trace; then `drain_graph_work` twice as
the daemon would):

```
REQ-MARKER caller_calls=[]  marker=1 available=False  queued=[retitled]
           daemon: drain1=2 [converge, rebuild_all, off_boundary]  drain2=0  left=[]
REQ-EPOCH  caller_calls=[]  available=False  queued=[retitled]
           daemon: drain1=1 (per-path, no whole-vault call)  drain2=0  left=[]
```

- The caller thread made zero whole-vault calls in both cases.
- The daemon drained each receipt once: the second drain returns 0, and the oracle
  matches a fresh rebuild.
- Standalone callers keep their join. The marker case runs `converge` inline and the
  epoch case runs `off_boundary` inline; both end available with
  `graph_rebuild_completed`.

## NEW CONCERN 2: in the live state, the request deferral is reported as a completed rebuild

**Where:** `src/exomem/epistemic_graph.py:9174-9185`. The pre-existing
`required is not None and report.get("deferred")` block runs *before* the new labels at
9191-9197.

**Scenario:** the vault has a durable graph checkpoint, the acknowledgement covers it,
and the graph is available. That is the ordinary state for a replayed receipt. The replay
proves a page stale, and the request defers. The report has `deferred`+`queued`, so the
dispatch enters the old block and `acknowledged.covers(required)` is true. It returns
`("completed", "graph_rebuild_completed")` even though:

- no rebuild ran;
- the graph is unavailable;
- the receipt is still queued.

As a result the request terminal (`writer_lease.py:445-453`) reports the graph as
converged, `whole_vault_attempted` is true for a pass that never ran, and
`index_sync.py:809-819` doesn't treat the result as a coverage code.

**Reproduction:** `_built` → `_write_floor(1)` + `_write_checkpoint(gen 1)` →
`rebuild_all` → `_publish_past_a_stale_row` → (marker, or an epoch patched to refuse) →
request replay.

```
AV marker=False result=completed/graph_rebuild_completed available=False queued=[retitled]
AV marker=True  result=completed/graph_rebuild_completed available=False queued=[retitled]
```

Two variants route correctly: a checkpoint newer than the acknowledgement, and an
unavailable predecessor. Both withdraw availability first, so they take the checkpoint
door, which returns `deferred/graph_repair_queued`.

**Minimal fix:** put this just before line 9174:

```python
if (
    required is not None
    and report.get("deferred")
    and report.get("queued")
    and _caller_can_carry_pending(vault_root, mutation_coordinator)
):
    return GraphDispatchResult("deferred", "graph_repair_queued", required)
```

That is the same code the checkpoint door uses, and it is a coverage code. I prototyped
it: both AV repros return `deferred/graph_repair_queued` with zero caller-thread passes
and one daemon drain. The existing suite plus my repros pass (37). Add an AV-shaped test,
red first.

Keep `required is not None` in the guard: a `deferred` result without a checkpoint is
rejected (`graph_dispatch_failed`). That is also why the no-checkpoint request result
stays `completed/graph_repair_queued_for_drain`. It is acceptable because it is not
covered as pending, but task 9.4 should say so.

## Also checked

- `{**rebuilt, "whole_vault": 1}` moved the return outside the `with`, still inside the
  `try`, so `GraphRebuildInProgress` handling is unchanged. Only `upsert_after_write`
  consumes `refresh_paths`.
- `marker_stands` is sampled before the drain. If another process clears the marker in
  between, a per-path drain gets labelled as a rebuild. That window is benign.

## Verification

- `test_graph_replay_currency.py`: 14 passed at the head (3 red against `544662f`'s
  source).
- The replay, deferred-queue, drain, index_sync, post-handoff, records-recall,
  trash-exclusion and media-worker suites: 320 passed.
- ruff F is clean, and `openspec validate --all --strict` gives 217 passed.

**Recheck 2 verdict: REQUEST_CHANGES** (NEW CONCERN 2).
