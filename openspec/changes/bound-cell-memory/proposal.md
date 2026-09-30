## Why

An Exomem Cloud cell's resident memory grows with the vault. On a vault of about 9,300 files (3.6 GB) a cell sits at 1.2–1.7 GiB RSS when idle, and its first index build was OOM-killed at 1536Mi, so cells now carry a 3Gi limit. RAM per node is the scaling bottleneck: at this rate one 8 GiB node holds only a handful of large vaults, and memory cannot be added indefinitely.

A code analysis at c44eaa0b, recorded in the knowledge base as "Where an Exomem Cloud cell's RAM goes", found that the vault's Markdown is not what occupies memory. The resident cost is:

- a fixed embedding model;
- allocator high-water that is never returned to the OS;
- several whole-corpus Python projections that duplicate data the SQLite sidecars already hold: the embedding matrix, the parsed-page cache, the semantic corpus context, and a Python BM25 fallback.

Moving canonical storage to SQLite would not change this. Serving from the existing sidecars does.

## What Changes

- **Measure first.** A profiling harness records tracemalloc snapshots and `/proc/<pid>/smaps_rollup` at fixed points: after import, after model load, after the first hybrid find, after the first governed write, and after a reaper tick. It runs against a large vault, and every later step is judged against it.
- **Allocator hygiene.** The hosted runtime bounds glibc arenas (`MALLOC_ARENA_MAX=2`) and returns freed memory to the OS (`malloc_trim(0)`) after model reaps and derived-index drain batches.
- **Hosted cells serve vectors from the vec0 backend.** The embedding matrix is never held in Python memory, and the change is gated by the golden floors and a latency check. The backend is already adopted under "Retrieval Architecture Changes Are Deferred Until Measured".
- **The parsed-page cache gains a byte budget** alongside its entry-count bound.
- **Whole-corpus projections become reapable, then leave memory.** The semantic corpus context is released on idle like the other RAM caches, and then serves page states and link maps from the lexical store's tables instead of whole-vault Python objects.
- **A busy lexical sidecar never triggers a whole-corpus Python BM25 build**, and the entity-registry cache keeps only the current and previous checkpoints.
- **Index builds stream.** The vector matrix is preallocated, bulk drains do not copy the whole matrix per file, and the identity census does not hold every page's text at once.

Explicitly out of scope, each needing its own decision:

- int8 re-embedding, which re-embeds every vault and needs a quality gate;
- mmap-shared model weights, which is task 5.4 of `swap-embedding-runtime-to-onnx`;
- canonical SQLite storage, which the analysis found gives no memory gain.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `hosted-tenant-cell`: a cell's resident memory is bounded independently of vault size, returns to a measured idle floor, and has a measured first-build peak.
- `find-recall-efficiency`: the parsed-page cache is bounded by bytes as well as entries, and a busy lexical sidecar never falls back to a whole-corpus Python BM25 build.

## Impact

- **Code:**
  - `src/exomem/model_reaper.py`, `src/exomem/embedding_index.py` and `src/exomem/vecstore.py`;
  - `src/exomem/find_corpus.py`, `src/exomem/semantic_contract.py`, `src/exomem/bm25.py` and `src/exomem/entity_registry.py`;
  - the hosted stage of the `Dockerfile`, and the hosted environment defaults.
- **Tests and benchmarks:** the new profiling harness, the golden floors, and the vec0 parity and latency gates.
- **Operations:** hosted cells keep the 3Gi limit until the harness shows the first-build peak is below it with headroom. Only then are cellctl's defaults lowered.
