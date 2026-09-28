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
