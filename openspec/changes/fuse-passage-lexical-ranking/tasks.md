# Tasks

## 1. Ranking

- [x] 1.1 Keep the lexical terms of the sidecar's chunk rows, maintained with each chunk write and caught up from the path log after a write without them.
- [x] 1.2 Fuse the lexical and dense rankings by reciprocal rank fusion, and report the dense score at each rank.
- [x] 1.3 Keep exact dense ranking for no `mode` and `mode: "vector"`, fuse for `mode: "hybrid"`, and refuse other modes with their correction.
- [x] 1.4 Prepare the lexical terms inline for a small sidecar, in the background for a large one, and at cell warm-up, with a retry delay after a failure on both paths.
- [x] 1.5 Report each result's own dense similarity as `similarity`, null outside the dense ranking.
- [x] 1.6 Report the lexical state under `diagnostics`, outside the summary.
- [x] 1.7 Publish a whole-index rebuild without deriving terms in its transaction, and leave the terms to the background backfill.

## 2. Proof

- [x] 2.1 Prove that a passage that dense similarity misses reaches the top `limit` through its stems.
- [x] 2.2 Prove that stale and unpublished lexical candidates are withheld and counted.
- [x] 2.3 Prove that a request with no `mode` keeps the exact dense ranking and its scores, and that `mode: "vector"` equals it.
- [x] 2.4 Prove that lexical candidates respect the request's tag filter.
- [x] 2.5 Prove that a chunk rewritten without the terms triggers is matched on its new text.
- [x] 2.6 Prove that `similarity` is null for a lexical match beyond the dense ranking.
- [x] 2.7 Prove that a rebuild derives no terms in its transaction, and that the backfill derives the new rows' terms.
- [x] 2.8 Prove that a failed backfill is reported under `diagnostics` and does not run again within the retry delay.

## 3. Default change

The release ships fusion on request and does not wait on this section.

- [x] 3.1 Measure top-15 key coverage of fused and dense ranking on the paired evaluation cases through the real implementation.
  - Evidence: 49 real chat questions from one tenant, run on a copy of that tenant's cell through the real implementation. Top-15 key coverage was 20 for fused ranking and 13 for dense ranking (+7, 95% interval [0, 14]).
  - At answer level, the key segment reached the answer context in 19 cases for the cell with fused ranking and in 18 cases for the calling service's own vectors.
- [ ] 3.2 Pass the passage latency qualification on the declared reference with `mode: "hybrid"`.
  - Not met on 22 November 2026. `benchmarks/passage-evidence/qualification-hybrid-2026-11-22.json` records 1,200 complete calls, 600 fused replies, and zero busy refusals.
  - Hybrid p95 was 234.578 ms with one caller (bar 200 ms) and 572.763 ms with four callers (bar 500 ms), so both bars fail.
  - The dense default in the same run measured p95 173.184 ms and 480.120 ms. The hybrid mean was +25.9 ms (95% interval [20.8, 31.4]) and +57.4 ms ([46.5, 70.0]) above dense.
- [ ] 3.3 Make fused ranking the default after a held-out decision run shows that it is non-inferior to dense ranking or better.
