# Tasks

## 1. Ranking

- [x] 1.1 Keep the lexical terms of the sidecar's chunk rows, maintained with each chunk write and caught up from the path log after a write without them.
- [x] 1.2 Fuse the lexical and dense rankings by reciprocal rank fusion, and report the dense score at each rank.
- [x] 1.3 Accept `mode: "hybrid"` and `mode: "vector"` on the passage level, and refuse other modes with their correction.
- [x] 1.4 Prepare the lexical terms inline for a small sidecar, in the background for a large one, and at cell warm-up.

## 2. Proof

- [x] 2.1 Prove that a passage that dense similarity misses reaches the top `limit` through its stems.
- [x] 2.2 Prove that stale and unpublished lexical candidates are withheld and counted.
- [x] 2.3 Prove that `mode: "vector"` keeps the exact dense ranking and its scores.
- [x] 2.4 Prove that lexical candidates respect the request's tag filter.
- [x] 2.5 Prove that a chunk rewritten without the terms triggers is matched on its new text.

## 3. Delivery

- [ ] 3.1 Measure top-15 key coverage of fused and dense ranking on the paired evaluation cases through the real implementation.
- [ ] 3.2 Pass the passage latency qualification on the declared reference with fusion on.
