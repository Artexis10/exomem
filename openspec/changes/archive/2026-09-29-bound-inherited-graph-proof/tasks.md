# Tasks

## 1. Red-first reproduction

- [x] 1.1 Build a graph in one process lineage, open it from a fresh one
      (`freshness.clear()`), and assert repeated `available()` proves at most
      once per freshness identity (valid and stale sidecars).
- [x] 1.2 Assert the drain queues exactly one rebuild for a declined inherited
      sidecar and does not re-prove it each pass.
- [x] 1.3 Assert the readiness probe never calls the proof and reports
      `unproven`, then the remembered verdict.
- [x] 1.4 Assert graph recall returns within a bound with the graph lane
      degraded while a proof is held, and uses the graph once the background
      proof lands.
- [x] 1.5 Run the new tests against the unfixed code and record each failure.

## 2. Fix

- [x] 2.1 Remember the public proof verdict per sidecar, stored metadata,
      sidecar file identity, and recall projection identity.
- [x] 2.2 Add the no-prove read mode and `availability_state()`.
- [x] 2.3 Readiness/coordination status uses the no-prove read; admit
      `unproven` in the public readiness payload.
- [x] 2.4 Graph recall and the find cache key use the no-prove read and
      schedule a single background proof.

## 3. Verification

- [x] 3.1 Existing epistemic-graph, graph-drain, readiness and find suites stay
      green.
- [x] 3.2 Lint, the public-artifact privacy gate, and
      `openspec validate --all --strict` before and after the archive. The
      full corpus runs in CI; the partial local run's only failures (three
      context-activation files) fail identically on unchanged `main`.
