## 1. Background proof

- [x] 1.1 `epistemic_graph._open_read_snapshot`: in a managed service, a `SINGLE_FLIGHT` reader with no remembered verdict and no running proof starts the one background proof and refuses as `unproven`.
- [x] 1.2 Run the background proof as a bulk pass under the thread name `exomem-graph-proof`, and add that name to the test-suite thread drain.
- [x] 1.3 Test: after managed admission, the first graph recall over an inherited sidecar does not run the proof on its thread, reports `graph` as degraded, and the next recall keeps the lane from the background verdict.

## 2. Specification

- [x] 2.1 Modify the `epistemic-graph` requirement on inherited sidecar proofs.
