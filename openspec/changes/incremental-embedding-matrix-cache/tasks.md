# incremental-embedding-matrix-cache — tasks

## 1. Cache engine (test-first)

- [x] 1.1 Write `tests/test_embedding_matrix_cache.py` (fabricated vectors, no
      torch): matrix loads once and is reused; in-process writes never force a
      reload (new file, chunk-count up/down, delete); spliced result equals a full
      reload; external write triggers exactly one reload; delete-to-empty keeps the
      zero-row shape; WAL is on; concurrent readers+writer stay correct; and an
      END-TO-END `find(mode="vector")` test (stubbed embedder) that asserts the
      vector lane actually ran and three distinct finds share one matrix load.
- [x] 1.2 `all_vectors()` reader-snapshot fix + extract the full load into
      `_load_all_rows()` (both `EmbeddingIndex` and `ClipIndex`).
- [x] 1.3 Per-index `RLock`; `upsert_file`/`delete_file` (and CLIP
      `upsert`/`upsert_frames`/`delete`) copy-on-write splice via `_patch_cache`
      instead of nulling; `len == shape[0]` invariant; self-heal to full reload on
      any splice exception.
- [x] 1.4 `_apply_sidecar_pragmas` (WAL / synchronous=NORMAL / busy_timeout) in
      both `_connect`s; soft-fail to default journal.
- [x] 1.5 `rebuild_all`: single wipe + `executemany` transaction, cache left cold.

## 2. Sharing / wiring

- [x] 2.1 Module memo + `get_embedding_index`/`get_clip_index` +
      `clear_embedding_indexes()` reset hook.
- [x] 2.2 Route every production construction site through the getters: `find.py`,
      `warmup.py`, `audit.py`, `audit_fix.py`, `corpus_aware.py`, `media_worker.py`,
      `backfill.py`, and the in-module writers.
- [x] 2.3 Clear the memo in `tests/conftest.py`'s `vault` fixture (beside
      `find.clear_cache()`).

## 3. Verification

- [x] 3.1 `uv run pytest -q` green (1162 passed, 11 skipped — optional-dep gated);
      `find._freshness_key` and the hot-find-cache suite untouched; scaffold leak
      guard green.
- [x] 3.2 `ruff check` introduces no new findings (only pre-existing advisory
      I001/BLE001 remain, matching the committed baseline).
- [x] 3.3 `openspec validate incremental-embedding-matrix-cache --strict` — valid.
- [ ] 3.4 Desk-side live smoke (torch env): `find(include_timings=true)` twice
      ~30s apart while a backfill writes the sidecar — the `vector` lane stays flat
      (no 13s spike).
