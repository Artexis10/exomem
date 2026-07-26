## 1. Regression Tests First

- [x] 1.1 Add a high-cardinality exact-unit regression that proves `limit=3` returns the deterministic top three while opening at most eight candidate parents and never walking the corpus; run it and record the expected RED failure.
- [x] 1.2 Add an access-policy regression proving rejected leading candidates expand to a later prefix without skipping across a concurrent catalog deletion, while retaining the existing post-filter-before-limit regression; run it RED.
- [x] 1.3 Add broad-cardinality harness tests for minimum-cardinality pass/fail, preflight request sizing, aggregate-only bucketing, and unchanged selective defaults; run them RED.

## 2. Bounded Retrieval Implementation

- [x] 2.1 Implement restart-from-zero geometric prefix expansion so separate catalog reads cannot skip or duplicate candidates across a moving offset boundary.
- [x] 2.2 Implement the gated bounded-prefix exact-unit fast path and stop after the requested number of eligible records; retain exhaustive behavior for post-filter plans and unlimited requests.
- [x] 2.3 Extend `category_recall_latency.py` with an explicit broad-cardinality profile while preserving report privacy and existing percentile thresholds.

## 3. Verification

- [x] 3.1 Run focused category algebra, semantic-unit recall, catalog error, and latency-harness tests with embeddings disabled; document any baseline-only failures separately.
- [x] 3.2 Run Ruff, `git diff --check`, OpenSpec validation, and the proportionate lean test suite.
- [x] 3.3 Obtain an independent reviewer pass focused on false-empty, ordering, access-policy, and stale-catalog regressions; address every important finding.
