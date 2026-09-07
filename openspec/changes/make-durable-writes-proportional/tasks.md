## 1. Establish the measured implementation contract

- [x] 1.1 Retain the current-main public workflow baseline and resolver timing reproduction; verify the responsible call chain and record compact sanitized evidence (`docs/benchmarks/proportional-writes-2026-09.md` and baseline JSON; privacy gate passed).
- [x] 1.2 Resolve an independent design critique before opening implementation lanes; validate the resulting OpenSpec change strictly (independent critic READY after completeness, prior-topology, membership, direct-key and isolation amendments).

## 2. Reuse proven writer metadata

- [x] 2.1 Add red-first coverage for a warm semantic corpus with a cold writer resolver cache; prove detached snapshot parity, no unrelated body reads, no preview publication, configuration-race fallback, unreadable non-governed membership, new direct-disk key after an unobserved title edit, and old-key/new-corpus rejection.
- [x] 2.2 Implement read-only corpus-entry reuse in the shared writer resolver seam; pass focused writer, corpus-cache, semantic creation and Records recall tests.
- [x] 2.3 Obtain independent review approval after the reviewer reruns the resolver reproduction and cache/policy race checks in isolated state (APPROVE on b94778e0; 124 scoped tests and seven independent adversarial scenarios passed).

## 3. Persist and query graph dependencies

- [ ] 3.1 Add red-first tests proving bounded forward-link discovery after reopening the index, including aliases, anchors, retitles, ambiguity, deletion and rename; compare public rows with full rebuilds and prove conservative recovery for unprovable outside-KB retitle/deletion ambiguity.
- [ ] 3.2 Populate source coverage and raw dependencies atomically in the graph sidecar, with schema recovery, full-rebuild clearing and delete/policy/purge handling; pass rollback, positive-hit-with-missing-dependent, corrupt target/key, isolation reopen and old-sidecar tests.
- [ ] 3.3 Replace full-body dependency discovery with indexed candidate queries while retaining independent publication proofs; pass focused deferred queue, graph freshness, Records and graph parity suites.
- [ ] 3.4 Obtain independent review approval after the reviewer reproduces topology parity, false-negative discovery and interrupted publication cases in isolated state.

## 4. Verify integrated behavior and deliver

- [ ] 4.1 Integrate reviewed lane bytes and obtain fresh integration approval with independently rerun writer-to-graph interaction checks.
- [ ] 4.2 Run at least three fresh paired public Markdown workflows at each of 3,800 and 8,000 pages against the pinned Basic Memory artifact, alternating product order; retain sanitized individual measurements and meet the median parity target in design.md.
- [ ] 4.3 Run the existing real-media workflow with isolated state and verify extraction, custody and useful closure remain correct; report optional graph convergence separately.
- [ ] 4.4 Pass the full lean test corpus, relevant latency gates, lint/type checks, public-artifact privacy validation and pinned strict OpenSpec validation on the final implementation bytes.
- [ ] 4.5 Commit the intended scope, integrate current remote main safely, push and open a ready Conventional Commit PR with measured behavior and validation evidence; retain the worktree for review follow-ups.
- [ ] 4.6 After an explicitly authorized merge, verify the remote default-branch result, synchronize and archive this change through OpenSpec, and remove only clean fully-pushed task worktrees with no live processes.
