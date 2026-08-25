## 1. Regression Tests

- [x] 1.1 Add production-sized keyword and hybrid cold-catalog tests proving typed warming and zero reference walks or page reads.
- [x] 1.2 Add warm-order tests proving both catalog scopes precede optional page and resolver work and quiet mode retains the catalog phase.
- [x] 1.3 Add runtime-readiness tests for warming, ready, and unverified states with no content leakage.

## 2. Implementation

- [x] 2.1 Gate large-corpus keyword and hybrid lexical lanes through non-walking catalog readiness and the existing typed warming outcome.
- [x] 2.2 Split maintained-catalog warming from optional caches and publish retrieval catalog readiness at the exact transition.
- [x] 2.3 Project content-free retrieval admission into runtime readiness and overall status.

## 3. Verification and Delivery

- [x] 3.1 Run focused retrieval, warmup, and readiness tests plus strict OpenSpec validation.
- [ ] 3.2 Run the lean suite, lint and types, latency gate, packaging checks, and review the actual diff.
- [ ] 3.3 Deploy the patch release to personal and POLLY, then verify cold and hot public recall, graph recovery, and memory telemetry.
