## 1. Graph Maintenance Regressions

- [x] 1.1 Add a failing full-rebuild test proving one detached resolver acquisition is reused across multiple pages without mutating the shared cache.
- [x] 1.2 Add failing batched-refresh tests proving one acquisition per batch and reacquisition across separate operations.
- [x] 1.3 Add failing failure-ordering and snapshot-parity tests covering existing-row preservation, ambiguous links, and isolation from shared-resolver patches.

## 2. Bounded Graph Implementation

- [x] 2.1 Acquire the detached resolver before graph mutation and thread it explicitly through full rebuild and batched refresh indexing.
- [x] 2.2 Keep direct edge-extraction compatibility while preventing graph maintenance from performing per-page resolver freshness work.

## 3. Freshness and Fanout Regressions

- [x] 3.1 Add failing freshness tests distinguishing missing baselines from seeded empty baselines and preserving independent scope drift.
- [x] 3.2 Add failing explicit-reconcile tests proving final exact live baselines are installed when event indexes are enabled.
- [x] 3.3 Add failing watcher integration tests proving an unchanged post-reconcile pass creates no fanout/receipts and the next real source change dispatches exactly once.

## 4. Freshness and Reconcile Implementation

- [x] 4.1 Make missing-baseline reconciliation initialize live state with an empty non-drift delta while preserving real deltas from existing baselines.
- [x] 4.2 Add final on-disk freshness rebaselining and use it from successful write-mode reconcile with safe fallback behavior.
- [x] 4.3 Preserve inbound/resolver cache correctness and event-index kill-switch behavior after rebaseline.

## 5. Verification and Delivery

- [ ] 5.1 Run focused graph/freshness/watcher/reconcile tests, Ruff, OpenSpec validation, and the lean suite while recording inherited baseline failures separately.
- [ ] 5.2 Run an independent review for spec conformance, race safety, and regression scope; address all actionable findings.
- [ ] 5.3 Benchmark a quiescent production reconcile, verify queue counts and derived-index drift remain zero, and confirm local/public health after deployment.
