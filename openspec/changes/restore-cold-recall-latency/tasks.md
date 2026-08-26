## 1. Regression tests

- [x] 1.1 Add a runtime-activation ordering test proving the file watcher seeds the recall projection before retrieval warm-up begins.
- [x] 1.2 Add readiness tests proving a repaired retrieval catalog can promote to ready and a lost live projection demotes it again.
- [x] 1.3 Add request-path tests proving every find mode, including vector mode, refuses a missing live projection without walking the vault while offline use retains one bounded fallback walk.
- [x] 1.4 Add timing-attribution coverage for recall-projection work.
- [x] 1.5 Add startup coverage proving managed warm-up never starts a second lexical rebuild owner while repair is in flight.
- [x] 1.6 Add scheduler coverage proving repeated stale probes cannot chain duplicate full-corpus rebuilds.
- [x] 1.7 Add failed/superseded publication coverage proving uncovered repair work survives a bounded idle handoff.
- [x] 1.8 Add post-proof race coverage proving an older publication cannot acknowledge a newer generation's repair request.
- [x] 1.9 Add real N-to-N+1 watcher catch-up coverage proving successful upsert, delete, and mixed changed/deleted batches persist their validated checkpoint and retry exact runtime admission after publication loses the promotion race.

## 2. Runtime fix

- [x] 2.1 Expose watcher seed completion and a trustworthy projection-live result.
- [x] 2.2 Start watcher observation and seeding before retrieval warm-up while keeping graph and media behind retrieval admission.
- [x] 2.3 Implement read-only retrieval-catalog readiness promotion and projection-loss demotion.
- [x] 2.4 Enforce the live-projection policy across all server find modes while preserving the explicit offline fallback.
- [x] 2.5 Attribute recall-projection work in find timings.
- [x] 2.6 Delegate managed startup catalogue recovery to the existing single-flight repair worker while retaining synchronous reconciliation for offline callers.
- [x] 2.7 Coalesce full-rebuild requests observed during an already-active full repair while preserving escalation from targeted repair.
- [x] 2.8 Skip catalogue-dependent optional cache warming while managed repair owns recovery and preserve uncovered full requests without chaining scans.
- [x] 2.9 Bind full-repair coalescing to the exact recall-generation pair proven by publication so a post-proof generation survives.
- [x] 2.10 Apply each watcher generation's changed/deleted union under one publication barrier, persist only its exact validated checkpoint, then re-prove managed retrieval read-only so a current catalogue cannot remain process-unavailable.

## 3. Verification and delivery

- [x] 3.1 Run focused tests, lint, and strict OpenSpec validation.
- [ ] 3.2 Run the proportional lean test suite and latency gates.
- [ ] 3.3 Build and install the candidate, then run distinct-query Windows acceptance against personal and POLLY cells.
- [ ] 3.4 Obtain independent adversarial review, resolve findings, synchronize and archive the OpenSpec change, then deliver through the repository release workflow.
