# Review: PR #1435 (activation latency and warm-up)

Head reviewed: `f8e494a`.

## REQUEST_CHANGES

One medium finding: bulk passes have no progress guarantee.

## Findings

### M1. Overlapping activations starve the rebuilds (`foreground_priority.py:215-224`, cap at `:32`)

- **What happens:** before every unit, a yield waits until `_in_flight == 0`, for at most 2 s. When requests overlap, `_in_flight` never reaches zero, so each unit pays the full 2 s.
- **Reproduction:** two threads loop over `foreground()` plus a 0.4 s sleep, offset by 0.2 s. Beside them, `bulk()` over `yielding_in_bulk(range(3))` took **6.00 s**, which is 0.5 units/s.
- **Scale:** both rebuilds yield per page per phase, so on thousands of pages minutes become hours.
- **Graph rebuild:** a longer pass widens the identity window (`epistemic_graph.py:3813-3861`). Concurrent writes then use up `REBUILD_STABILIZATION_*`, and the pass declines to publish.
- **Lexical repair:** while it is stuck, retrieval stays unadmitted, and every activation also pays L1's proof.
- **Fix:** add a progress floor. Either give each `bulk()` a yield budget (for example, total wait ≤ 50 % of elapsed time) or run N units without yielding after each capped wait. Add a test that asserts progress under continuous overlapping foreground work.

### L1. Unadmitted requests each pay a proof (`find.py:1246`)

- **What happens:** once the required warm stages finish, every request that is not admitted runs `runtime_retrieval_catalog_current`, which takes reserved-state locks. Before this change, the preload window returned a cheap `warming`.
- **Fix:** single-flight the proof, one per `_retrieval_generation`.

### L2. Readiness probes queue without bound (`server_assets.py:115`)

- **What happens:** the limiter bounds running proofs at 2, but waiters queue without limit. Starlette does not cancel a handler when the client disconnects, so after a 30 s stall, 30 queued polls replay their proofs back to back.
- **Fix:** coalesce onto the in-flight proof, or answer 503 when `available_tokens == 0`.

### L3. The item 7 crash window has no test (`vault.py:972`)

- **What I checked:** I made `_run_after_release` drop its work (a death before fan-out). After `clear_stores()` and `reset_memo()`, `search_bm25` found the new term, so reconcile converges.
- **Fix:** commit this as a regression test.

### L4. The hold summary flushes lazily (`mutation_lock.py:460-506`)

A window is emitted only on the next quiet hold, so the last window is lost when holds stop. **Fix:** flush from the metrics snapshotter.

**Nit:** `working_set_heat.py:1076` calls heat "derived"; it is recorded telemetry.

## Hypotheses cleared

- **H1. Readiness honesty.**
  - `finish_required_warm` (`warmup.py:512`) runs after catalogue, graph_handoff, semantic_corpus and lexical. It is not in a `finally`, so an exception leaves the flag unset and readiness fails closed.
  - It bumps `_retrieval_generation`, so the CAS at `readiness.py:249` discards older proofs.
  - Ready still requires the strict proof.
  - With 2 slots, at most two proof threads can run, and request work cannot starve them.
- **H2. The warm stays out of caller state and continuity.**
  - I ran the real `_warm_request_path` on a seeded vault in normal and quiet modes. The log shows "done in 851 ms".
  - Heat sessions were unchanged, and no file under the vault or `XDG_STATE_HOME` changed.
  - With no attribution and no anchor, `note_selection` and `note_session` never run.
  - `episode_nudge._caller` returns None without a transport.
- **H3. No yield under a lock.** I checked all 9 call sites.
  - The lexical publication lock is a `vault_creation_lock` (`lexstore.py:3509`), which `_holds_a_boundary` detects. The same check covers in-process boundaries, including reserved-state ones.
  - Transactions that stay open across a yield are only on temp or private sidecars. The only caller of `_rebuild_all_pass` is `temporary_index` (`:3083`).
  - `LexicalStore._lock` is taken only after publish.
  - `_local.depth` stops a thread from waiting on itself.
- **H4. Log levels.**
  - Overdue holds are forced to INFO.
  - A wait ≥ 25 ms (the poll interval) or a hold ≥ 250 ms stays INFO, as do command, control and unknown holder kinds.
  - Refusals are unchanged and stay WARNING.
  - `_reset_hold_summary` runs via `register_at_fork` and replaces the object, so an inherited lock cannot deadlock.
- **H5. Trims.**
  - **Manifest cache:** `access.is_indexable` runs before the cache (`recall_policy.py:216`), so a policy change is never cached. The parse is a pure function of its inputs, and `CollectionError` is a `ValueError`.
  - **Heat durability:** heat runs in WAL mode (`:1109`), so `synchronous=NORMAL` loses at most the last commits on power failure and does not corrupt the file; corruption rebuilds anyway.
  - **Skipped abstention marks:** the thread is kept exactly as the SQL branch keeps it. A skip requires the same workspace and client, the staleness bound of 300 s is far below `SESSION_GAP_NS` (6 h), and a clock step backwards forces a write.
- **H6. Item 1 coverage.** `is_embeddable_path` is the embedder's own predicate. The PR's test shows `[page, log.md, index.md]` is accepted with only the page queued, and that `[log.md, index.md]` alone still escalates (fails closed). No full receipt is minted.
- **H7. Fan-out ordering.**
  - The fan-out stays inside the vault-wide `VaultMutationCoordinator.hold`.
  - `_commit_existing_locked` reads `reports` only for its return value, which is patched afterwards (`semantic_writes.py:2792`).
  - `move_file` reads its reports after the lock is released (`:958`).
  - An error from the body is not masked: `sys.exc_info()` is set inside the generator's `finally` (checked on 3.11).

## Verification

- Scratch probes (not committed): M1 fails at 6.00 s, and the warm probe and the crash probe both pass.
- PR scoped suites: 325 passed, 11 skipped.

## Recheck at `aa053fe`

Round diff: `git diff f8e494a aa053fe`. It touches `foreground_priority`, `readiness`, `server_assets`, `vault`, `mutation_lock`, `metrics` and `working_set_heat`, plus five test files. The rest is a merge of `origin/main` (OpenSpec/docs only).

### Findings

- **M1 is resolved (`foreground_priority.py:42-129`).**
  - The outermost `bulk()` gets a `_PassBudget`. A wait is capped at `2 s + 0.5 × elapsed − waited`, and a wait that hits its cap earns `ceil(wait / unit cost)` units that run without yielding.
  - A direct `yield_to_foreground()` inside the pass, such as the graph `_rebuild_all_pass`, draws on the same budget.
  - Free units are spent before the boundary check, which is harmless because a free unit never waits.
  - Measured with my probe under two request streams that keep `_in_flight ≥ 1`:
    - 3 units took 2.00 s (the single grace wait), down from 6.00 s.
    - 1 s of work finished in 5.02 s; 4 s of work finished in 6.02 s (66 % bulk share).
    - Both are within the bound `elapsed ≤ 2 × (work + 2 s)`, so over long passes bulk keeps at least half the wall time.
  - The new tests (`test_foreground_priority.py:318, :352`) pin both the floor and the priority of a lone request.
- **L1 is resolved (`readiness.py:263-308`).** Proofs are single-flight per (vault, generation, admitted), and a finished flight is never reused. Sharing cannot launder a stale proof: any invalidation bumps the generation, and the CAS at `:245` drops the result.
  - *Residual, low:* a sharer that waits more than 30 s returns `False`. On an `admitted=True` key, that clears admission because a proof was slow, not because it failed. The owner's later `True` is then discarded by the CAS. This is a readiness flap, but it fails closed, so it stays honest. A possible follow-up: on a sharer timeout, return `admission` unchanged instead of `False`.
- **L2 is resolved (`server_assets.py:123-147, 214`).** Probes coalesce onto one future per digest. `asyncio.shield` keeps a disconnect from cancelling the shared proof, and the done-callback forgets the flight, so there is no queue and no replay.
  - *Nit:* sharers return the first probe's `traffic` snapshot.
- **L3 is resolved.** `test_a_death_before_the_released_fanout_still_converges_on_restart` is the same shape as my probe. The PR reports that it fails when reconcile is stubbed.
- **L4 is resolved.** `flush_hold_summary` is registered on the snapshotter tick and skips an empty window. The flush reads the module global, so the fork reset still applies.
- **The nit is resolved (`working_set_heat.py:1078`).**
- **New code: `vault._run_in_captured_context` (`:856-870`).**
  - It copies back every var the deferred fan-out changed, last write wins.
  - The only such var is `graph_sync._PENDING_WAITERS`, a copy-on-write dict. The only writers are the graph dispatch functions (`epistemic_graph.py:5418, 8590, 8870, 8879, 9134`), and these run in the fan-out, never in the locked body after commit.
  - Where two fan-outs share one lock, both write the same (vault, state root) key, and the later one wins, as it would inline. This is equivalent today.
  - *Nit:* copying back only when the releasing context still holds the captured value would make that ordering explicit.

### CI-regression fixes: no assertion weakened

- **`test_read_after_write_visibility.py`:** only the setup changed. `_publication_barrier_held_elsewhere` produces real contention on the publication barrier, and `deferred == [[page]]` plus the during-repair read are unchanged. The old route (`VAULT_LOCK_NESTED`) is the bug that item 7 removed.
- **`test_membench_trackd` j3:** the test is untouched. The source fix above restores the graph rebuild registration, and two new red→green tests pin it end to end (`asserted_pairs` returns the pair).
- **Windows timeout:** no test or source change. The PR cites main run 36413687175 failing the same way at a docs-only commit, and the job passed at `aa053fe`.
- **Test lines:** the round's test diffs add 313 lines and delete 1. The deleted line is the `_governed_transition` call, re-indented under the barrier; no assertion was removed.

### CI (PR #1435 check runs, head `aa053fe`)

- 36 check runs: 26 success, 10 skipped, 0 failures.
- "required CI gate" is green. It covers all 12 core shards, the 4 harness shards, Windows held filesystem (NTFS), product E2E, lint, OpenSpec and package build.
- The skipped jobs are workflow-gated, including the templated matrix names.

### Local verification

- Scoped suites at `aa053fe`: foreground_priority, fanout_after_creation_lock, read_after_write_visibility, readiness_after_required_warm, readiness_honesty, mutation_lock, membench_trackd, working_set_heat, post_promotion_warm and index_sync. Result: **265 passed, 11 skipped**.
- Floor probe results as above.

### Verdict: APPROVE

M1 and L1–L4 are fixed and tested, the CI fixes keep every assertion, and CI is green. The two residual items (the sharer-timeout flap and copy-back ordering) are low and fine as follow-ups.
