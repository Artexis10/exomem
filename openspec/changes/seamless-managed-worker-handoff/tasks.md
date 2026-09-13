## 1. Phase 1: stop the post-handoff rebuild loop

- [ ] 1.1 Turn the reproduction into failing regression tests: a live watcher with one unattributed edit before each of ten governed writes, asserting zero whole-vault rebuilds, incremental acknowledgement within a stated bound, and the graph repair queue draining to zero; record the failing output before implementation.
- [ ] 1.2 Apply the external-pending fence only to reads that require a current projection; route an unreadable predecessor to queued incremental repair with a distinct outcome; record external marks per path; pass 1.1 and the existing graph, freshness, watcher and alarm suites.
- [ ] 1.3 Add failing tests for a bounded standalone join (request-deadline-derived and default budgets, pending outcome past the budget) and for rebuild-demand coalescing (ten writes during one flight yield at most one follow-up rebuild); implement both and pass the graph locking, drain and idempotency suites.
- [ ] 1.4 Add a supervisor-harness test that performs a real worker handoff and asserts the first ten writes after promotion are incremental with no whole-vault rebuild; pass the managed-service e2e, ingress and service-manager suites.
- [ ] 1.5 Update `docs/` for the changed outcomes and the doctor's guidance; regenerate any derived artifact the outcome vocabulary touches; run the scoped suites, lint, strict OpenSpec validation and the privacy gate.
- [ ] 1.6 Obtain an author-independent review of the phase-1 diff covering the fence narrowing, the predecessor-outcome split, path-scoped marks, the join budget and coalescing; resolve findings and recheck; open a ready pull request with the evidence and merge under the standing authority.
- [ ] 1.7 Release, deploy through the managed upgrade, and verify on the personal service that writes after the handoff acknowledge within the bound with no rebuild sidecar chain and a draining repair queue; record the numbers.

## 2. Phase 2: standby warm-up and promotion

- [ ] 2.1 Add failing tests for standby readiness (`cutover` component set; no lease, publication or scheduler while standby), the separate warm budget with discard-and-record on expiry, and promotion order including the declared-migration skip and the re-proof after a migration; implement in the supervisor, worker runtime, readiness and warm-up modules.
- [ ] 2.2 Add failing tests for stream reattachment retrying through promotion within the cutover budget; implement in the ingress.
- [ ] 2.3 Add failing tests for background vocabulary recovery draining after activation once the projection is current; implement in the activation sequence.
- [ ] 2.4 Re-measure recall during a single rebuild after phase 1 on a production-sized fixture; if the degradation still exceeds the stated bound, implement the child-process rebuild with reduced priority behind the existing single-flight owner and its in-process fallback reporting; otherwise record the measurement and close this task as not needed.
- [ ] 2.5 Update the release operator scripts and `docs/` for the standby sequence and budgets; enable model preload in the personal service environment as a recorded operator step.
- [ ] 2.6 Obtain an author-independent review of the phase-2 diff covering ownership during standby, promotion ordering, budget expiry and stream retry; resolve findings and recheck; open a ready pull request and merge under the standing authority.
- [ ] 2.7 Release and deploy; verify on the personal service that the upgrade after this one keeps the endpoint usable throughout, with the measured unavailable window, cutover component timings and first-write latency recorded; then synchronize and archive this change.
