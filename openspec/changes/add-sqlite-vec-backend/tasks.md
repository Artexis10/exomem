## 1. Tests First

- [x] 1.1 Add `tests/test_vecstore.py` (module-level `pytest.importorskip("sqlite_vec")`,
      random normalized vectors, no torch): vec0 schema creation is idempotent; the
      load-failure memo makes a failed extension load permanent-per-process and cheap.
- [x] 1.2 Add migration/backfill tests: a sidecar built blobs-only (forced-numpy env) gains
      populated vec0 tables with matching counts on first use under `auto`.
- [x] 1.3 Add drift-heal tests: manually deleted/extra vec rows are restored to count
      lockstep by the next sync check.
- [x] 1.4 Add the f32 parity test: over N=500 random vectors and 20 queries, vec0-f32
      `search()` returns the same ordered (path, chunk) top-k with matching scores (float
      tolerance) as the numpy path.
- [x] 1.5 Add binary+rescore tests: planted-nearest-neighbor clusters are recovered in
      top-k and returned scores are exact cosine (computed from f32 blobs, not Hamming).
- [x] 1.6 Add dual-write lockstep tests for `upsert_file`/`delete_file`/`rebuild_all` and
      Clip `upsert`/`upsert_frames`/`delete`, including the image-only replace
      (`frame_ts IS NULL`) case: blob and vec counts stay equal after every mutation.
- [x] 1.7 Add lean-safe fallback tests (no importorskip; must pass with sqlite-vec absent):
      `EXOMEM_VEC_BACKEND=numpy` forces the numpy path; a forced load-failure memo serves
      correct results via numpy with the unchanged return shape.
- [x] 1.8 Add the quantized-mode golden pass: `test_retrieval_golden.py` parametrized over
      `EXOMEM_VEC_QUANT=off|binary`, both passes clearing the floors and the per-query
      zero-recall cliff guard.

## 2. vecstore Module

- [x] 2.1 Implement `src/exomem/vecstore.py`: `SqliteVecStore(source_table, vector_column,
      dim, vec_table)` with `try_load` (per-connection, process-global failure memo),
      `ensure_synced` (create-if-missing + count-mismatch rebuild-from-blobs, memoized per
      instance), dual-write delete/insert helpers, `wipe`, and `knn` (f32 cosine; binary
      Hamming with k*8 candidates + f32 rescore).
- [x] 2.2 Implement `backend()` / `quant_mode()` env readers (`EXOMEM_VEC_BACKEND`,
      `EXOMEM_VEC_QUANT`) and keep the module import-safe without sqlite_vec installed.

## 3. EmbeddingIndex Integration

- [x] 3.1 Load the extension in `_connect()` via `vecstore.try_load` (no-op after memo).
- [x] 3.2 Dual-write vec rows in `upsert_file` / `delete_file` / `rebuild_all` inside the
      existing transactions, vec deletes before blob deletes.
- [x] 3.3 Implement the `search()` ladder: kill switch → availability → binary+rescore →
      f32 KNN → numpy on any vec exception (log once, mark instance unavailable).
- [x] 3.4 Add `vector_backend_active(vault_root)` helper for warmup/doctor.

## 4. ClipIndex Integration

- [x] 4.1 Mirror 3.1–3.3 for `ClipIndex` (`vec_images`, float[512]/bit[512]), carrying the
      `frame_ts IS NULL` predicate into the image-only replace's vec delete.

## 5. Warmup and Doctor

- [x] 5.1 Branch `warmup.warm_caches()`: vec backend active → sync check + one dummy KNN;
      otherwise prime the numpy matrix as today.
- [x] 5.2 Add doctor checks: `sqlite-vec` dependency presence (embeddings extra) and an
      in-memory loadability probe, reported as warn (numpy fallback exists), for the
      hybrid/media profiles.

## 6. Benchmark Harness and Scale Story

- [x] 6.1 Extend `scripts/latency_curve.py` with `--vec-backend` (comma list:
      numpy,sqlite-vec,binary — per-backend passes over the same built sidecar, index memo
      cleared between passes) and `--corpus-cache DIR` (vault+sidecar reuse keyed by
      (n, links_per_note, seed)); report per-backend vector-lane latency, top-10 overlap
      vs numpy, and peak-RSS delta when psutil is importable.
- [ ] 6.2 Run the numpy baseline tiers (10k/50k/100k, real embeddings, desk-side) before
      the backend lands; re-run all three backends after; both recorded.
- [ ] 6.3 Write the `docs/benchmarks.md` "Vector lane at scale" section: before/after
      tables, overlap@10, memory story, and the explicit hnswlib decision point.

## 7. Validation

- [ ] 7.1 Lean suite: `uv run python -m pytest -q` with `KB_MCP_DISABLE_EMBEDDINGS=1` and
      no extras — vecstore tests skip cleanly, fallback tests run.
- [ ] 7.2 Retrieval eval: `uv run --extra embeddings python -m pytest -m embeddings` —
      golden floors hold in f32 (exactness) and binary (gated) configurations.
- [ ] 7.3 `ruff check`.
- [ ] 7.4 `openspec validate --specs --strict`.
- [ ] 7.5 End-to-end: `find` against a real vault under each `EXOMEM_VEC_BACKEND` value —
      identical top hits f32-vs-numpy, no degradation recorded on fallback.
