# Design - sqlite-vec vector backend

## Context

`EmbeddingIndex.search()` (`src/exomem/embeddings.py`) computes `matrix @ query_vec` over a
process-cached (N, 768) float32 matrix loaded from the `chunks` table of
`.embeddings.sqlite`; `ClipIndex.search()` is a near-duplicate over `images` in
`.clip.sqlite` (512-dim). The matrix cache is mtime-invalidated and incrementally spliced by
`_patch_cache()` after in-process writes. `audit.py`'s corpus-contradiction sweep reuses the
same matrix via `all_vectors()`. Fusion (`fusion.py`) consumes ranked path lists only, so
the vector backend can change beneath `search()` without touching `find.py` or fusion.

Two facts anchor the design, verified against sqlite-vec's own documentation:

1. Stable sqlite-vec (v0.1.x) is a SIMD-optimized brute-force scan, NOT an ANN index — its
   scale levers are quantization (`bit[N]` columns, `vec_quantize_binary`) and keeping the
   scan in C over on-disk pages instead of a Python-resident matrix. Its ANN work
   (DiskANN/IVF) is alpha-only and not depended on here.
2. Raw `np.float32.tobytes()` blobs — exactly what `chunks.vector`/`images.vector` already
   store — are sqlite-vec's native float32 input format, so vec0 tables can be populated
   and rebuilt from the existing blobs with pure SQL, no model in the loop.

## Goals / Non-Goals

**Goals:**

- Remove the Python-resident O(N) matrix (memory + cold-load) from the default vector
  search path at scale, keeping results exact.
- Keep the sidecar-file model: vec0 tables live inside the existing sidecars; blob tables
  stay the source of truth.
- Keep every freshness seam intact: writer hooks, file watcher, reconcile, and the
  find-cache sidecar-mtime key all continue to work unchanged.
- Provide an opt-in quantized mode whose recall is gated by the golden retrieval floors.
- Measure, before and after, at 10k/50k/100k-note scale with real embeddings.

**Non-Goals:**

- No ANN index (HNSW/IVF/DiskANN) in this change — sqlite-vec's stable release has none.
- No change to `find` ranking, request/response shapes, fusion, or the command surface.
- No removal of the numpy path: it remains the fallback, the kill-switch target, and the
  substrate for `audit.py`'s all-pairs sweep.
- No re-embedding: migration and rebuild never invoke the embedding model.

## Decisions

### One shared vec0 helper, composed into both index classes

`EmbeddingIndex` and `ClipIndex` are duplicated implementations; a base-class refactor is
riskier than the feature. A new `src/exomem/vecstore.py` module owns everything vec0:
extension loading (with a process-global load-failure memo mirroring `_IMPORT_FAILED`),
schema creation, count-sync/rebuild-from-blobs, dual-write SQL, and KNN (float and
binary+rescore). Each index class holds a `SqliteVecStore` instance parameterized by
(source table, vector column, dim, vec-table name). The module is import-safe without
`sqlite_vec` installed — the import is lazy inside the load probe.

Alternative considered: extract a shared base class for both indexes first. Rejected —
it rewrites the two most correctness-sensitive classes in the codebase to save ~50 lines,
and the composition seam gives CLIP the same backend with no inheritance risk.

### vec0 rowid == blob-table rowid; join back for metadata

Both `chunks` and `images` are ordinary rowid tables (composite TEXT PKs, not WITHOUT
ROWID), so every row has a stable implicit rowid within a transaction's lifetime. vec0
rows are inserted with the blob row's rowid (`INSERT INTO vec_chunks(rowid, embedding)
SELECT rowid, vector FROM chunks WHERE file_path = ?`) and KNN results join back to the
blob table for metadata. No metadata/auxiliary columns in vec0 — deleting by rowid is the
dependable primitive, and duplicating `file_path` per vec row buys nothing.

Caveat (documented, not defended in schema): VACUUM can renumber rowids of tables without
an INTEGER PRIMARY KEY. Nothing in the codebase VACUUMs sidecars; if anything ever does,
the count-sync check cannot detect a pure renumbering, so a VACUUM of a sidecar must be
followed by a vec rebuild (`audit_fix(rebuild_embeddings=True)` does this implicitly).

Alternative considered: an explicit `id INTEGER PRIMARY KEY` on the blob tables. That is a
schema migration of every existing sidecar for a hazard no current code path can trigger.

### Same-transaction dual-write; vec deletes precede blob deletes

Every mutator already wraps its work in `with conn:`. The vec delete must run BEFORE the
blob delete (`DELETE FROM vec_chunks WHERE rowid IN (SELECT rowid FROM chunks WHERE
file_path = ?)`) because the subquery needs the blob rows; the vec insert runs after the
blob insert, selecting the fresh rowids. `ClipIndex.upsert`'s image-only replace carries
the same `AND frame_ts IS NULL` predicate into the vec-delete subquery. `rebuild_all`
wipes vec tables alongside `chunks` and repopulates with one whole-table
`INSERT ... SELECT` after the bulk blob insert. Dual-writes are skipped entirely when the
extension is unavailable on the writing process — the sync check heals later.

Alternative considered: SQLite triggers on the blob tables. Triggers fire for every writer
including ones without the extension loaded (they would hard-fail lean writers), and they
hide write behavior from the code that owns it.

### Count-mismatch sync-on-first-use is both migration and drift healer

On an index instance's first vec use per process (memoized), after ensuring the tables
exist: compare `count(chunks)` to `count(vec_chunks)`; on mismatch, wipe and repopulate
vec rows from blobs in one transaction, logged at INFO. A pre-existing sidecar (vec count
0 ≠ N) is thereby migrated on first open with no model and no user action — the same
idempotent-on-connect pattern as `ClipIndex._migrate_add_frame_ts`. A sidecar advanced by
a non-vec-aware writer (old version, lean CLI) is healed the same way. Because dual-writes
are same-transaction, counts cannot diverge under normal operation; a runtime KNN failure
additionally drops that call to the numpy path and marks the instance unavailable for the
process.

Alternative considered: a schema-version table with explicit migrations. Heavier than the
one invariant we need (vec rows ≡ blob rows), and count-compare self-heals cases a version
number cannot see (partial manual surgery).

### Backend ladder with explicit env surface; quantization strictly opt-in

`EXOMEM_VEC_BACKEND` = `auto` (default) | `sqlite-vec` | `numpy` (kill switch).
`EXOMEM_VEC_QUANT` = `off` (default) | `binary`. In `auto`, availability alone decides —
no corpus-size threshold, because the f32 scan is exact at every N (the per-query
difference at small N is milliseconds against a ~800 ms find budget) and one fork fewer is
one fork tested. Binary mode runs Hamming KNN over `bit[N]` with `k * 8` candidates, then
rescores those candidates against their f32 blobs in numpy and returns true cosine scores,
so downstream consumers see the same score semantics in every mode.

`distance_metric=cosine` on the float vec0 column keeps `score = 1.0 - distance` on the
existing cosine scale (vectors are L2-normalized at embed time).

Alternative considered: auto-enable binary above a corpus-size threshold. Rejected — a
recall-affecting mode flipping implicitly as a vault grows makes results non-reproducible
across time; the golden gate can bless the mode, but turning it on stays a user decision.

### The numpy matrix stays; it just stops being loaded when vec0 serves search

`all_vectors()` / `_load_all_rows()` / `_patch_cache()` are untouched: `audit.py`'s
all-pairs sweep and the fallback ladder need them. When vec0 serves search, `search()`
never touches the matrix cache, so it stays cold and `_patch_cache()` early-returns on
writes — the memory win requires no cache surgery. `warmup.warm_caches()` branches: when
the vec backend is active it runs the sync check plus one dummy KNN (faulting in the vec0
pages); otherwise it primes the matrix as today.

### Fallback substrate if measurement disappoints: hnswlib

If the 10k–100k tier curves show vec0-f32 not beating the numpy scan and binary
quantization unable to close the gap within the golden floors, the documented fallback is
a true ANN library (hnswlib) behind the same `vecstore` seam — a separate index file
beside the sidecar, real `M`/`ef_search` tuning against the golden gate. Not implemented
in this change; recorded so the decision point is explicit in `docs/benchmarks.md`.

## Risks / Trade-offs

- f32 mode doubles vector bytes on disk (~3 KB/chunk duplicated into vec0 shadow tables)
  -> accepted: sidecars are local per-machine dotfiles; binary mode adds only ~96 B/chunk,
  and at the scale where storage matters binary is the mode you run.
- Python built without loadable-extension support (`enable_load_extension` missing) ->
  process-global soft-fail memo catches `AttributeError`/`OperationalError`; numpy path
  serves; `doctor` surfaces loadability as a warn.
- Binary quantization recall loss -> default-off; rescore-at-full-precision; golden floors
  (NDCG@10 ≥ 0.85, MRR ≥ 0.80, recall@10 ≥ 0.90, per-query zero-recall cliff guard) run in
  the quantized configuration as its promotion gate.
- Mixed-version writers (a process without the extension writes blobs only) -> count
  drift, healed by sync-on-first-use in the next vec-aware process.
- Windows is not CI'd; the extension loads or it doesn't per machine -> verified working
  on the maintainer's Windows box (vec_version v0.1.9); every failure mode soft-fails to
  numpy; lean CI matrix exercises the genuinely-absent path on every run.
- Float32 dtype discipline: a float64 array's `.tobytes()` is read as 2× the dimensions
  and vec0 rejects it -> all writes go through the existing `.astype(np.float32)` sites;
  vecstore asserts dtype at its boundary.

## Migration Plan

No user action. Existing sidecars gain vec0 tables on first use by a vec-aware process
(rebuild-from-blobs, seconds even for large sidecars, no model). Rolling BACK is equally
safe: older code ignores the extra vec0 tables entirely (SQLite only errors when a virtual
table is queried without its module), and blob tables never stopped being the source of
truth. `audit_fix(rebuild_embeddings=True)` rebuilds both table families.

## Open Questions

None for implementation. The hnswlib decision is deferred to the measured curve in
`docs/benchmarks.md`.
