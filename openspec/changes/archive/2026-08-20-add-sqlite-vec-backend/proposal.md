## Why

The vector lane is an O(N) in-memory scan: `EmbeddingIndex.search()` loads the entire
(N, 768) float32 matrix into Python RAM (~3 KB per chunk resident — roughly 1 GB at a
100k-note corpus) and re-stacks it from the sidecar whenever the sidecar changes out of
process. The `find-recall-efficiency` spec deliberately deferred any retrieval-architecture
change "until the timing diagnostics justify it"; that measurement now exists — the per-lane
timing diagnostics and the latency-vs-scale curve harness (`scripts/latency_curve.py`) show
the vector lane and its matrix load are the lanes whose cost grows linearly with corpus
size. exomem is OSS: strangers will point it at corpora far larger than the maintainer's
vault, and "search visibly degrades at scale" is a weak scale story.

## What Changes

- Add `sqlite-vec` (vec0 virtual tables) INSIDE the existing `.embeddings.sqlite` and
  `.clip.sqlite` sidecars — no new files, preserving the "just your local sidecar" model.
  The blob tables (`chunks`, `images`) remain the source of truth; vec0 rows are always
  rebuildable from stored blobs with pure SQL (no model, no re-embedding).
- Sidecar writers dual-write vec0 rows in the same transaction as blob rows; a
  count-mismatch check on first use rebuilds vec0 from blobs — one mechanism serving as
  both the migration for pre-existing sidecars and the drift healer.
- `search()` gains a backend ladder: vec0 full-precision scan (exact — rank-identical to
  the in-memory scan) → opt-in binary-quantized scan with exact full-precision rescore →
  the existing numpy scan.
- Extend `scripts/latency_curve.py` with per-backend vector-lane measurement
  (`--vec-backend`) and a reusable corpus cache (`--corpus-cache`) so 10k/50k/100k-note
  tiers with real embeddings are runnable desk-side; publish the resulting scale story in
  `docs/benchmarks.md`. These tiers are opt-in desk-side runs, never CI.
- `doctor` reports sqlite-vec presence and extension loadability.

Defaults and soft-fail, stated explicitly: `EXOMEM_VEC_BACKEND` defaults to `auto` — the
vec0 full-precision backend serves vector search when the extension is importable and
loadable, and it is exact (result-identical to the numpy path), so `auto` changes no
ranking. Every vec failure mode — package missing (lean install), Python's sqlite3 built
without loadable-extension support, or a runtime error — soft-fails to the numpy path with
zero behavior change; `EXOMEM_VEC_BACKEND=numpy` is the kill switch.
`EXOMEM_VEC_QUANT` (binary quantization) is default-OFF and strictly opt-in because it can
affect recall; the golden retrieval floors are its promotion gate.

Pure-substrate note: sqlite-vec is a distance calculator over vectors the server already
measured — no model runs, nothing is generated or judged. This is the same measurement,
computed in SQL instead of numpy.

## Capabilities

### New Capabilities

- None.

### Modified Capabilities

- `find-recall-efficiency`: the vector lane gains a SQL-native KNN backend (exact
  full-precision default, opt-in quantized mode gated by the golden floors, kill switch,
  soft-fail to the in-memory scan); the "architecture changes are deferred until measured"
  requirement is updated to record that the measurement justified this backend; the
  matrix-cache and warm-up requirements are scoped to whichever backend serves search.
- `live-index-freshness`: sidecar writes now keep vec0 tables transactionally in sync with
  the blob tables, with a rebuild-from-blobs self-heal for sidecars written by
  non-vec-aware processes.

## Impact

- Code: `src/exomem/vecstore.py` (new shared vec0 helper), `src/exomem/embeddings.py`
  (both index classes: connect hook, dual-writes, search ladder), `src/exomem/warmup.py`,
  `src/exomem/doctor.py`, `scripts/latency_curve.py`, `docs/benchmarks.md`.
- Surfaces: none — no command-registry, MCP, REST, or CLI parameter changes; `find`'s
  request/response shapes and ranking are unchanged.
- Tests: new vecstore unit suite (schema, sync, migration, drift heal, f32 parity vs the
  numpy scan, binary rescore, dual-write lockstep); lean-suite fallback coverage (kill
  switch, forced-unavailable); a quantized-mode pass of the golden retrieval gate.
- Dependencies: `sqlite-vec>=0.1.6` added to the `embeddings` extra (plain PyPI wheel).
  The base install is unaffected; lean deployments never import it.
