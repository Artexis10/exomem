# Tasks

## 1. Measure (D1)

- [x] 1.1 Profiling harness: drive a cell process through import, model load, first hybrid find, first governed write, and a reaper tick. Record tracemalloc top sites and total, `smaps_rollup`, cache-status counters, and unreturned allocator memory. The report is content-free.
- [x] 1.2 A large synthetic vault fixture, and a CI lane that runs the harness and publishes the report.
- [ ] 1.3 Baseline: run the harness on the synthetic vault and on a copy of a real large vault. Record the per-consumer figures and confirm or correct the analysis note's ranking.

## 2. Quick wins (D2, D3, D4, D6); each PR carries a harness before/after

- [x] 2.1 Allocator hygiene: set `MALLOC_ARENA_MAX=2` in the hosted image, and call a guarded `malloc_trim(0)` after model reaps and drain batches. Harness (bge-m3 int8, 3,000 synthetic notes): after a reap, RSS falls to 549 MiB from 1273 MiB and unreturned allocator memory to 270 MiB from 1000 MiB; the arena bound alone saves 30–60 MiB at each earlier point.
- [x] 2.2 vec0 for hosted cells: default to `EXOMEM_VEC_BACKEND=sqlite-vec` in hosted mode, verify the image loads the extension, and refuse at start with a stable code when it cannot. Gates: golden floors, backend parity, and latency at the owner-vault chunk count.
  - Superseded: service-v1 cells no longer hold the corpus matrix. Chunk recall and write advisories use bounded exact disk scoring over ordered SQLite vector blocks (add-cloud-service-resource-policy 3.5, #1533). `vecstore` keeps `numpy` as the default and `sqlite-vec` opt-in; an ANN or vec0 backend is evaluated separately in #1492.
- [x] 2.3 Byte-bounded `FrontmatterCache` with an environment override; the hosted default is set from 1.3.
  - Evidence: `FrontmatterCache` in `src/exomem/find_corpus.py` is byte-bounded by `_page_cache_bytes()`, with a 32 MiB service-profile default and the `EXOMEM_PAGE_CACHE_BYTES` override (#1533, add-cloud-service-resource-policy 3.6). The default came from the service-v1 cell measurements rather than 1.3.
- [x] 2.4 `bm25.warm` declines when FTS5 is present but busy or unsynced, with a red-first test for the locked-sidecar case. The harness path never has a busy sidecar, so the locked-sidecar test is the evidence; the saving is the in-process BM25 cache that is no longer built.
- [x] 2.5 Entity-registry cache holds 2 checkpoints, not 16. Bounded by construction and pinned by its test; the harness's single write never creates a second checkpoint.

## 3. Structural (D7, D5 step 2)

Service-v1 cells no longer load the full matrix (see 2.2), so 3.1 now affects only local and legacy processes. 3.1–3.4 and 4.x stay open. They become launch work only if the Cloud occupancy qualification (add-cloud-service-resource-policy 3.9) shows corpus-context or rebuild memory limiting density. The 3 GiB cell limit holds until measurements support a reviewed change.

- [ ] 3.1 Streaming matrix load (preallocate from the row count), and bulk drains that reload once instead of splicing per file.
- [ ] 3.2 Batched `rebuild_all` and a streaming identity census.
- [ ] 3.3 Serve semantic page states and link maps from the lexical store tables. The semantic-contract suites must stay green, and the harness must show corpus-context bytes no longer scaling with the vault.
- [ ] 3.4 (was 2.6) Register the semantic corpus context as a reapable idle cache slot. Blocked on D5 step 2 (3.3).

## 4. Rollout

- [ ] 4.1 Run the harness against the release candidate. Record the idle floor and first-build peak for the owner-vault copy.
- [ ] 4.2 Lower cellctl's default cell memory limit and request to the measured peak plus headroom (platform chart). This re-renders every cell, so roll it out outside the backup window.
