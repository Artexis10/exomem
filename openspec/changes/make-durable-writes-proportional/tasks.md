## 1. Establish the measured implementation contract

- [x] 1.1 Retain the current-main public workflow baseline and resolver timing reproduction; verify the responsible call chain and record compact sanitized evidence (`docs/benchmarks/proportional-writes-2026-09.md` and baseline JSON; privacy gate passed).
- [x] 1.2 Resolve an independent design critique before opening implementation lanes; validate the resulting OpenSpec change strictly (independent critic READY after completeness, prior-topology, membership, direct-key and isolation amendments).

## 2. Reuse proven writer metadata

- [x] 2.1 Add red-first coverage for a warm semantic corpus with a cold writer resolver cache; prove detached snapshot parity, no unrelated body reads, no preview publication, configuration-race fallback, unreadable non-governed membership, new direct-disk key after an unobserved title edit, and old-key/new-corpus rejection.
- [x] 2.2 Implement read-only corpus-entry reuse in the shared writer resolver seam; pass focused writer, corpus-cache, semantic creation and Records recall tests.
- [x] 2.3 Obtain independent review approval after the reviewer reruns the resolver reproduction and cache/policy race checks in isolated state (APPROVE on b94778e0; 124 scoped tests and seven independent adversarial scenarios passed).

## 3. Persist and query graph dependencies

- [x] 3.1 Add red-first tests proving bounded forward-link discovery after reopening the index, including aliases, anchors, retitles, ambiguity, deletion and rename; compare public rows with full rebuilds and prove conservative recovery for unprovable outside-KB retitle/deletion ambiguity.
- [x] 3.2 Populate source coverage and raw dependencies atomically in the graph sidecar, with schema recovery, full-rebuild clearing and delete/policy/purge handling; pass rollback, positive-hit-with-missing-dependent, corrupt target/key, isolation reopen and old-sidecar tests.
- [x] 3.3 Replace full-body dependency discovery with indexed candidate queries while retaining independent publication proofs; pass focused deferred queue, graph freshness, Records and graph parity suites.
- [x] 3.4 Obtain independent review approval after the reviewer reproduces topology parity, false-negative discovery and interrupted publication cases in isolated state (APPROVE on 84f3d01a after typed-identity, Windows-WAL and stale-census corrections; final targeted reviewer run: 24 passed).

- [x] 3.5 Resolve independent critique of the evidence-backed foreground/background scan amendment before implementation (READY after making waiter callbacks lock-free and expressing the pause limit as a monotonic requested-wait budget with overshoot tests).
- [x] 3.6 Add red-first tests for exact-vault activity, nested/exception cleanup, fork-child lock/scope reset, no foreground or self-wait, bounded background progress (including fake-clock sleep budgets and scheduling overshoot), lock-free dynamic graph-waiter callbacks, and unchanged graph/due-state convergence (18 focused tests pass; independent fork and interrupted-entry reproductions are fixed).
- [x] 3.7 Register complete foreground dispatcher invocations and enable cooperative checkpoints only in the existing graph and due-state background workers; preserve all publication and admission proofs (reviewed through 836e3014 and integrated).
- [x] 3.8 Obtain independent implementation review and rerun the measured public workflow without diagnostic patches (APPROVE on 836e3014 after fork-state and interrupted-entry corrections; separate uninstrumented 3,800-page workflow passed in 7.74 s, followed by all 12 paired acceptance observations).

## 4. Verify integrated behavior and deliver

- [x] 4.1 Renew integration approval after the foreground/background amendment, including writer-to-graph and vocabulary composition checks (APPROVE on exact measured 2b172de1; 78 composition checks plus 20 fresh integration checks passed; subsequent 7c961455 test corrections independently passed 29 scoped tests).
- [x] 4.2a Correct the shared benchmark's incomplete-index readiness defect: retain invalid-for-scale observations, prove exact metadata/text-index fixture membership for both products before timing, and independently review the correction and rejection tests (APPROVE on 6c3b7d9d; 45 tests, wrong-vault/plain-table rejection reproductions, bounded-startup rejection, and real four-page public workflows for both products passed).
- [x] 4.2 Run at least three fresh paired public Markdown workflows at each of 3,800 and 8,000 pages against the pinned Basic Memory artifact, alternating product order; retain sanitized individual measurements and meet the median parity target in design.md (all 12 observations passed; Exomem/Basic Memory medians 8.57/13.73 s at 3,800 and 14.37/18.53 s at 8,000; independent evidence audit passed 411 checks).
- [x] 4.3 Run the existing real-media workflow with isolated state and verify extraction, custody and useful closure remain correct; report optional graph convergence separately (8,000-page real PDF/OCR run passed: three preserved hashes and expected extracts, citations and final reads; useful closure 43.58 s, graph still warming. Separate graph-context-available setup passed the common workflow in 11.52 s; it does not prove global graph convergence).
- [ ] 4.4 Pass the full lean test corpus, relevant latency gates, lint/type checks, public-artifact privacy validation and pinned strict OpenSpec validation on the final implementation bytes.
- [ ] 4.5 Commit the intended scope, integrate current remote main safely, push and open a ready Conventional Commit PR with measured behavior and validation evidence; retain the worktree for review follow-ups.
- [ ] 4.6 After an explicitly authorized merge, verify the remote default-branch result, synchronize and archive this change through OpenSpec, and remove only clean fully-pushed task worktrees with no live processes.
