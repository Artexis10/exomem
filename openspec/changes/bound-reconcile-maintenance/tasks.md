## 1. Graph Maintenance Regressions

- [x] 1.1 Add a failing full-rebuild test proving one detached resolver acquisition is reused across multiple pages without mutating the shared cache.
- [x] 1.2 Add failing batched-refresh tests proving one acquisition per batch and reacquisition across separate operations.
- [x] 1.3 Add failing failure-ordering and snapshot-parity tests covering existing-row preservation, ambiguous links, and isolation from shared-resolver patches.
- [x] 1.4 Add deterministic stabilization regressions for target creation after snapshot acquisition and repeated two-pass churn.
- [x] 1.5 Mutate the shared cached resolver after detached acquisition and prove the active graph pass remains isolated.
- [x] 1.6 Add failure-ordering regressions for retry freshness/resolver acquisition and partial-pass failures while preserving initial acquisition behavior.
- [x] 1.7 Add marker-publication regressions for stable passes, post-check failure, an already-admitted refresh overlapping a failed rebuild, and missing-sidecar refresh.
- [x] 1.8 Add a spawned-process serialization regression and assert graph coordination state is vault-rooted and shared.
- [x] 1.9 Add regressions for walker exclusion, pre-mutation lock failure, and degraded index-sync reporting of structured lock errors.
- [x] 1.10 Add deterministic reader races proving complete-old snapshots before rebuild and unavailable/empty results after marker removal.

## 2. Bounded Graph Implementation

- [x] 2.1 Acquire the detached resolver before graph mutation and thread it explicitly through full rebuild and batched refresh indexing.
- [x] 2.2 Keep direct edge-extraction compatibility while preventing graph maintenance from performing per-page resolver freshness work.
- [x] 2.3 Bracket full rebuild passes with disk-truth freshness, retry once on movement, and mark repeated churn unavailable before raising.
- [x] 2.4 Invalidate every exceptional exit after a rebuild pass starts without invalidating failures before the first mutation boundary.
- [x] 2.5 Make stable full-rebuild completion the only schema-version publisher and route non-current incremental refresh through a full rebuild.
- [x] 2.6 Serialize all public graph mutators with one vault-rooted re-entrant OS-backed mutation coordinator.
- [x] 2.7 Exclude coordination state from both walkers and propagate structured lock failures to the index-sync degradation boundary.
- [x] 2.8 Route every trusted graph read through one read-only validated SQLite transaction without taking the mutation lock.

## 3. Freshness and Fanout Regressions

- [x] 3.1 Add failing freshness tests distinguishing missing baselines from seeded empty baselines and preserving independent scope drift.
- [x] 3.2 Add failing explicit-reconcile tests proving final exact live baselines are installed when event indexes are enabled.
- [x] 3.3 Add failing watcher integration tests proving an unchanged post-reconcile pass creates no fanout/receipts and the next real source change dispatches exactly once.
- [x] 3.4 Add symmetric exact-once deletion recovery coverage after explicit rebaseline.

## 4. Freshness and Reconcile Implementation

- [x] 4.1 Make missing-baseline reconciliation initialize live state with an empty non-drift delta while preserving real deltas from existing baselines.
- [x] 4.2 Add final on-disk freshness rebaselining and use it from successful write-mode reconcile with safe fallback behavior.
- [x] 4.3 Preserve inbound/resolver cache correctness and event-index kill-switch behavior after rebaseline.
- [x] 4.4 Document and retain lock-held final baseline installation so a post-scan event patch cannot be overwritten.

## 5. Verification and Delivery

- [ ] 5.1 Run focused graph/freshness/watcher/reconcile tests, Ruff, OpenSpec validation, and the lean suite while recording inherited baseline failures separately.
- [ ] 5.2 Run an independent review for spec conformance, race safety, and regression scope; address all actionable findings.
- [ ] 5.3 Benchmark a quiescent production reconcile, verify queue counts and derived-index drift remain zero, and confirm local/public health after deployment.
