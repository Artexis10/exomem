## 1. Regression tests

- [x] 1.1 Add a runtime-activation ordering test proving the file watcher seeds the recall projection before retrieval warm-up begins.
- [x] 1.2 Add readiness tests proving a repaired retrieval catalog can promote to ready and a lost live projection demotes it again.
- [x] 1.3 Add request-path tests proving every find mode, including vector mode, refuses a missing live projection without walking the vault while offline use retains one bounded fallback walk.
- [x] 1.4 Add timing-attribution coverage for recall-projection work.

## 2. Runtime fix

- [x] 2.1 Expose watcher seed completion and a trustworthy projection-live result.
- [x] 2.2 Start watcher observation and seeding before retrieval warm-up while keeping graph and media behind retrieval admission.
- [x] 2.3 Implement read-only retrieval-catalog readiness promotion and projection-loss demotion.
- [x] 2.4 Enforce the live-projection policy across all server find modes while preserving the explicit offline fallback.
- [x] 2.5 Attribute recall-projection work in find timings.

## 3. Verification and delivery

- [x] 3.1 Run focused tests, lint, and strict OpenSpec validation.
- [ ] 3.2 Run the proportional lean test suite and latency gates.
- [ ] 3.3 Build and install the candidate, then run distinct-query Windows acceptance against personal and POLLY cells.
- [ ] 3.4 Obtain independent adversarial review, resolve findings, synchronize and archive the OpenSpec change, then deliver through the repository release workflow.
