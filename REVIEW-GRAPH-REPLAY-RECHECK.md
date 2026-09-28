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

## Recheck 3 (head `41a103b`)

Reviewed `99aae8b..41a103b`: `epistemic_graph.py` (+10), `tests/test_graph_replay_currency.py`
(+86) and task 9.4 (+8).

| Item | Status | Evidence |
|------|--------|----------|
| NEW CONCERN 2 | **FIXED** | The new guard (`epistemic_graph.py:9174-9183`) runs before the old checkpoint-deferred block and matches the Recheck 2 prototype. In the acknowledged-checkpoint repro, marker and epoch both now give `deferred/graph_repair_queued`, `whole_vault_attempted=False`, and a code in `_GRAPH_COVERAGE_CODES`. |
| `index_sync` coverage | **FIXED** | The code is a coverage code, so the result is treated as covered by durable per-path receipts. |
| Request terminal | **PARTIAL** | The graph diagnostic reads `deferred/graph_repair_queued`. **Correction to Recheck 2:** the graph is not a `derived_sync` component (`mutation_terminal.py:112`). The terminal's `graph_sync` field comes from `writer_lease._durable_graph_outcome`, which returns `completed` whenever the acknowledgement covers the checkpoint (`writer_lease.py:1204`). In this repro it returns `completed` while the graph is unavailable and the receipt is queued. That is pre-existing: the external-pending door produces the same state. It is also narrow: a request that writes Knowledge Base markdown advances the checkpoint and takes the correct checkpoint door (`graph_sync.py:1600-1633`). Follow-up rather than blocker, but the new test's name claims "reports pending", while it asserts only the diagnostic. |
| Task 9.4 | **Accurate for this round** | Its evidence lists omit the regressions below. |

**Thread-spy repros**, rerun on the head:
- Mutation-request trace, all four variants (marker/epoch × no checkpoint/acknowledged
  checkpoint): zero whole-vault calls on the caller thread. Each receipt drains exactly
  once, by the daemon (second drain returns 0), and the graph matches a fresh rebuild.
- Standalone callers keep their join: `graph_rebuild_completed`,
  `whole_vault_attempted=True`, and the graph ends available.

## BLOCKER: CI is red on `41a103b`, with regressions from `a52b2a6` onward

The PR's check runs show 6 failed jobs: `lint + targeted types` and core shards 1, 4, 5,
9 and 10. I reproduced both of the following locally. Every test named here passes on
`origin/main` and with the base `53ac15a` source.

**1. mypy** (`lint + targeted types`):
- `epistemic_graph.py:5236`: `set(drain_scope)`, where `drain_scope` is typed `int`.
- `epistemic_graph.py:5587`: `"_drain_after_release": sorted(...)` inside a
  `dict[str, int]`.

Fix: give the refresh report a `dict[str, Any]` or TypedDict, or pass the scope outside
the report.

**2. NEW CONCERN 3: the proof skips reconcile's reprojection.**
`_replayed_path_currency` proves a page current from the file row's `source_hash`
alone. Reconcile deliberately calls a refresh on *unchanged* markdown to reproject
derived rows. The log shows `proved replayed paths current count=1 delta_paths=0`, and
nothing is repaired. Failures:
- `test_semantic_unit_reconcile.py::test_reconcile_repairs_unit_drift_without_markdown_changes_and_is_idempotent`
  (`'degraded' == 'repaired'`)
- `...::test_hierarchy_reconcile_refreshes_derived_state_without_rewriting_markdown`
- `...::test_explicit_upgrade_reconcile_reprojects_unchanged_rich_units_everywhere`
  (graph unit rows stay `stale-generation`)

This is a correctness regression: unit drift and parser upgrades no longer repair.
Minimal fix: make the proof also compare the page's stored unit/projection generation,
or give reconcile and explicit repair a way to bypass the proof. Keep the no-op only
for replayed receipts.

**3. Tests that relied on refreshing an unchanged page.** The new no-op now
short-circuits them before they exercise their property:
- `test_epistemic_graph_freshness.py::test_refresh_batch_reuses_one_snapshot_and_separate_calls_reacquire`
  (no resolver acquisition)
- `...::test_incremental_refresh_retries_when_path_changes_during_indexing`
  (`raced is False`)
- `test_freshness_liveness_contract.py::test_publication_failure_records_graph_recovery_state_not_vault_freshness`
  (no publication attempt, so no fence)

Update each one so its page is actually changed and in the delta, which keeps the
intent. Don't skip them.

Bisect: the two freshness tests fail at `a52b2a6`, `0d69219`, `5f68cf9`, `544662f` and
`99aae8b`, and pass at `53ac15a`. My earlier rounds' scoped suites didn't include these
files, and neither did the PR's evidence.

## Verification

- A sweep of the 66 graph, index_sync, deferred, freshness, reconcile, move, trash,
  media-worker and durable-closure files: **6 failed**, 1627 passed, 18 skipped. The 6
  failures are all listed above.
- `test_graph_replay_currency.py` passes, `openspec validate --all --strict` gives 217
  passed, and ruff F is clean. mypy on `epistemic_graph.py` fails with 2 errors (clean
  on main).

**Recheck 3 verdict: REQUEST_CHANGES** (red CI: mypy, the reconcile regression in
NEW CONCERN 3, and three obsolete test setups).

## Recheck 4 (head `b722f62`)

Reviewed `41a103b..52da51d`: `epistemic_graph.py`, `index_sync.py`, the spec delta,
task 9.4/9.5 and three test files. The head then moved to `b722f62`, whose only change
is a line wrap of the unit tuple in `_stored_units_current`. Local results below are
from `52da51d`; CI is from `b722f62`.

| Item | Status | Evidence |
|------|--------|----------|
| mypy | **FIXED** | `refresh_paths` and `_refresh_paths_locked` now return `dict[str, Any]`. Run locally with the CI command, `epistemic_graph.py` has no errors. The 5 remaining errors are missing `yaml` stubs in my environment, identical on `origin/main`. CI `lint + targeted types` passes. |
| NEW CONCERN 3 | **FIXED** | The proof runs only when `outside and replayed` is true (`epistemic_graph.py:5561`). Every other caller hits the restored `caller_path_outside_delta` fallback. `replayed=True` is passed only by `drain_deferred_work`'s batch and isolation dispatches (`index_sync.py:1307,1328`), through `index_sync.upsert_after_write` to the graph dispatch. `_stored_units_current` compares `(node_key, parent_generation, parser_version)` with the stored semantic-unit rows; `parent_source_hash` is covered by the file-hash check before it. All three `test_semantic_unit_reconcile.py` failures pass. The new unit-generation replay test would fail without the check. |
| Freshness tests | **PASS unmodified** | Both `test_epistemic_graph_freshness.py` tests and the liveness-contract test pass with their original setup. |
| Task 9.4 / 9.5 | **Accurate** | 9.4 records this round. 9.5 is left open for the `_durable_graph_outcome` follow-up from Recheck 3, which keeps the change active, as it should be. |

**The no-checkpoint label** (`completed/graph_repair_queued_for_drain`): nothing
mislabels it.
- `index_sync.full_upsert_succeeded` skips `completed` components
  (`index_sync.py:801`), so the full receipt clears while the graph receipt stays queued.
  That is correct custody: the graph queue owns the repair, and no extra whole-component
  refresh is minted.
- The request terminal's `graph_sync` comes from `_durable_graph_outcome`, which returns
  `None` when there is no checkpoint (`writer_lease.py:1201-1203`). No graph field is
  written, so nothing claims convergence.
- `whole_vault_attempted` is False, and no `src/` code matches on either code string.
- The only leftover is the terminal diagnostic entry, which reads `state: completed`
  next to an honest code. That is cosmetic, and the type rejects `deferred` without a
  checkpoint.
- Replays now run only in the file-watcher daemon and the CLI (the only callers of
  `drain_deferred_work`), never under a request, so request terminals rarely see this.

**Thread-spy repros** rerun on `52da51d` (marker/epoch × no checkpoint/acknowledged
checkpoint):
- Replayed request callers: zero whole-vault calls on the caller thread in all four
  variants. The daemon's first drain clears the receipt, the second returns 0, and the
  graph matches a fresh rebuild.
- Non-replay callers: they keep `main`'s inline fallback, now truthfully labelled
  `graph_rebuild_completed` instead of `incremental_completed`.
- Standalone joins are unchanged.

**Verification**
- The same 66-file sweep as Recheck 3: **1635 passed, 18 skipped, 0 failed** (was 6
  failed).
- The reconcile, replay and freshness files: 111 passed.

**PR CI on `b722f62`** (run 36455436570): `required CI gate` succeeded; lint + targeted
types, OpenSpec validation, capabilities doc, package build, Windows NTFS, product E2E,
onboarding, terminal UI, core shards 1-12 and harness shards 1-4 all passed; Conventional
Commit title passed; the remaining jobs were skipped by workflow condition. No failures.

**Recheck 4 verdict: APPROVE.** The one residual item, `_durable_graph_outcome`
reporting `completed` under an acknowledged checkpoint, is tracked as open task 9.5 and
does not block this PR.
