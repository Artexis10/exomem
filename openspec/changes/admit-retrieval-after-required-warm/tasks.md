## 1. Readiness after the required warm

- [x] 1.1 Red-first `tests/test_readiness_after_required_warm.py`: a revoked catalogue is re-proved once the required warm is done while optional preloads run; before that it still waits; a failing proof still fails closed; a new warm clears the mark; `warm_all` marks the required warm finished before any model preload.
- [x] 1.2 `readiness.finish_required_warm()` / `required_warm_finished()`; `retrieval_admission` re-proves under either mark; `warm_all` calls it after the lexical stage; a refused recall request re-proves once the mark is set.
- [x] 1.3 Red-first: the readiness probe does not block the event loop; `/health/ready` runs `runtime_readiness` on a dedicated two-slot limiter.
- [ ] 1.4 Live: on the next promotion, readiness returns within seconds of the catalogue proof and does not wait on the reranker or CLIP preload.
