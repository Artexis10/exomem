## 1. Carry start-up validation across promotion

- [ ] 1.1 Add failing tests: a promoted worker whose handoff was carried on a current verdict runs `finish_startup_recovery` without the source-bytes proof and its completion record names `startup_validation`; a cold worker still runs the full proof; a promotion that did not carry the handoff runs the full proof.
- [ ] 1.2 Add failing tests for admission under contention: start-up validation meeting an unacknowledged checkpoint while a governed write holds the boundary for longer than the budget waits for that write's release, admits the published graph, suspends no reads and registers no rebuild; the same check with the boundary free still suspends and rebuilds.
- [ ] 1.3 Implement 1.1 and 1.2 in `file_watcher.py`, `warmup.py` and `service_standby.py`; extend the `seed` arm of `test_a_promoted_standby_serves_its_first_ten_writes_incrementally` with a write in flight during start-up validation, asserting zero whole-vault registrations.

## 2. Let the drain retire the marks it re-derives

- [ ] 2.1 Add a failing test: a process starting with a path-scoped external mark and queued receipts under it, with no watcher event, converges the checkpoint through the drain within the drain's own backoff and readers requiring a current projection are served; an unscoped mark still keeps the drain refusing.
- [ ] 2.2 Implement in `epistemic_graph.drain_paths`, `index_sync` and `freshness` (retire inside the drain's hold, path-scoped, compare-and-swap); pass the graph deferred-queue, drain, freshness and product E2E suites.

## 3. Closure

- [ ] 3.1 Author-independent review of the diff covering the carried validation, the contention branch and the drain's mark retirement; resolve findings and recheck; open a ready pull request and merge under the standing authority.
- [ ] 3.2 Release, deploy through the managed upgrade with a governed write issued during the promoted worker's start-up window, and record promotion-to-admitted-graph and the absence of a whole-vault rebuild; run the product E2E restart step ten times; then synchronize and archive this change.
