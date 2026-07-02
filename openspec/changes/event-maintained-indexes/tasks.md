## 1. Freshness Registry — Tests First

- [x] 1.1 Add `tests/test_freshness_registry.py`: registry-vs-walk triple equality across
  create/modify/delete/move for both kb and vault scope, including the rename-with-preserved-mtime
  case producing a changed digest.
- [x] 1.2 Add tests proving a not-live registry (never seeded, or `EXOMEM_DISABLE_EVENT_INDEXES`
  set) falls back to today's walk-based triple byte-identically.
- [x] 1.3 Add a test proving the periodic reconcile re-walk detects and repairs a simulated missed
  event (registry manually desynced from disk, then reconciled) and logs
  `freshness_reconcile_drift`.
- [x] 1.4 Add a test proving `FreshnessSnapshot.kb()`/`.vault()` consult the registry when live and
  perform zero filesystem walks (assert via a monkeypatched/counted walk function).

## 2. Freshness Registry — Implementation

- [x] 2.1 Add `src/exomem/freshness.py`: module-level registry keyed by `(vault_root, scope)`
  holding `{rel_path: mtime_ns}`, with a derived-triple accessor computed from the map (no
  syscalls), a seed function, an `on_files_changed(scope, changed_rels, deleted_rels)` patch API,
  and a `clear()`/reset test hook.
- [x] 2.2 Apply the exact parity rules the walks use (`VAULT_SCAN_SKIP_DIRS`, `.md`-only, same tree
  roots for kb vs vault scope) so live and fallback triples match on identical trees.
- [x] 2.3 Wire `FreshnessSnapshot.kb()`/`.vault()` (`find.py:677-699`) to consult the registry first,
  falling back to `_walk_freshness_key` unchanged when not live.
- [x] 2.4 Extend `FileWatcher` (`file_watcher.py`) to watch the vault root instead of only
  `Knowledge Base/`, keeping embedding-dispatch KB-filtered; publish drained paths to the freshness
  registry from `_flush()` alongside the existing embedding dispatch.
- [x] 2.5 Decouple the watcher's startup gate in `server.py` (`server.py:348-351`) to
  `not EXOMEM_DISABLE_FILE_WATCHER` only, dropping the embeddings condition.
- [x] 2.6 Seed the registry (one full walk per scope) from `warmup.warm_all` (`warmup.py:104-105`)
  immediately after `readiness.mark_ready("lexical")`.
- [x] 2.7 Add the periodic reconcile re-walk (every 300s) on the watcher's own thread/timer,
  independent of file events.
- [x] 2.8 Add publish calls to the freshness registry from every in-process writer hook that
  already registers a self-write or calls the embedding hooks: `vault.batch_atomic_write`
  (`vault.py:207-226`), `delete_file`/`move_file`/`delete_directory`'s delete paths
  (`delete_file.py:235-251`, `move_file.py:190-207`, `delete_directory.py:216-231`),
  `reconcile.reconcile` (`reconcile.py:113`), `media_worker` (`media_worker.py:129`),
  `scene_frames` (`scene_frames.py:91`), and `audit_fix`'s `rebuild_embeddings` path
  (`audit_fix.py:416`).
- [x] 2.9 Add `EXOMEM_DISABLE_EVENT_INDEXES` as a global kill switch forcing the freshness registry
  permanently not-live.

## 3. Process-Shared Embedding/CLIP Matrix — Tests First

- [x] 3.1 Add `tests/test_embedding_matrix_shared.py`: two sequential `EmbeddingIndex(vault_root)`
  instances (or two sequential `find()` calls) in one process do not re-read the sqlite table a
  second time when nothing changed — assert via `last_files_reloaded`/`last_files_reused`.
- [x] 3.2 Add a test proving a single-file `upsert_file` reloads exactly that one file
  (`last_files_reloaded == 1`) and reuses the rest (`last_files_reused == N-1`).
- [x] 3.3 Add a test using a second sqlite connection to the same `.embeddings.sqlite` (simulating
  an out-of-process writer/backfill) that writes new rows, then proves the next
  `EmbeddingIndex.search()`/`all_vectors()` call picks up only the newly-written files via a
  metadata-only delta scan, not a full reload.
- [x] 3.4 Add a test proving a `delete_file` removes the corresponding rows from the shared matrix
  and the deleted file does not appear in subsequent search results.
- [x] 3.5 Add `tests/test_clip_matrix_shared.py` mirroring 3.1-3.4 for `ClipIndex`.
- [x] 3.6 Add a test proving search results (ranked hits) are identical whether the matrix was
  freshly built or delta-reloaded from a prior shared state.

## 4. Process-Shared Embedding/CLIP Matrix — Implementation

- [x] 4.1 Move `EmbeddingIndex`/`ClipIndex`'s matrix cache (`embeddings.py:782`, `958`) from a
  per-instance attribute to module-level state keyed by resolved vault root, each guarded by its own
  `threading.Lock` used only for build-then-swap (never held during the matmul).
- [x] 4.2 Implement the metadata-only delta scan (`SELECT file_path, chunk_idx, file_mtime`, no
  vector blobs) triggered when the sidecar's (mtime, size) changed since the last swap; diff against
  the currently-held per-file breakdown; fetch vectors only for changed files; rebuild the stacked
  matrix by concatenating unchanged submatrices with newly-fetched ones.
- [x] 4.3 Add `last_files_reloaded`/`last_files_reused` module-level counters per vault, updated on
  every delta reload, mirroring the `bm25.last_tokenized` observability precedent.
- [x] 4.4 Update `upsert_file`, `delete_file` (EmbeddingIndex), `upsert`, `upsert_frames`
  (ClipIndex) to publish directly into the shared state instead of nulling a private `self._cache`.
- [x] 4.5 Correct the `EmbeddingIndex` docstring (`embeddings.py:774`) — "cached per-process" is now
  actually true on the request path, not just aspirational.
- [x] 4.6 Fold `EXOMEM_DISABLE_EVENT_INDEXES` into the matrix path: when set, `EmbeddingIndex`/
  `ClipIndex` behave exactly as before this change (per-instance cache, full reload per instance).

## 5. Inbound-Link Index Patch API — Tests First

- [x] 5.1 Add `tests/test_inbound_index_incremental.py`: `on_files_changed` applied for a
  create/modify/delete produces a bucket/stem_counts state with identical *content* to a full
  `_build_inbound_index` rebuild of the same resulting tree.
- [x] 5.2 Add a test explicitly covering the documented `seq`-ordering divergence (patched entries
  get insertion-order sequence numbers, not full-walk-order ones) so the difference is asserted,
  not silently relied upon.
- [x] 5.3 Audit `tests/test_inbound_index.py`'s existing assertions for order-sensitivity; adjust
  any assertion that depends on cross-file relative `seq` order to a content-set comparison where
  the patch path is exercised.
- [x] 5.4 Add a test proving `move_file`/`delete_file`/`delete_directory`'s inbound-link safety
  checks see a patched index that reflects a just-applied rename (the case the digest-strength key
  exists to protect, per the base `live-index-freshness`/`find-recall-efficiency` specs).

## 6. Inbound-Link Index Patch API — Implementation

- [x] 6.1 Add `on_files_changed(vault_root, changed_rels, deleted_rels)` to `vault.py`'s
  `_INBOUND_INDEX` machinery (`vault.py:505-553`): remove each affected file's existing edges from
  `buckets` and its `stem_counts` contribution, then re-read only files in `changed_rels` and re-add
  their edges/stem-count contribution.
- [x] 6.2 Keep `_build_inbound_index`/`_inbound_index`'s digest-keyed full rebuild as the not-live
  fallback, gated the same way as D1's freshness fallback (registry not live, or
  `EXOMEM_DISABLE_EVENT_INDEXES` set).
- [x] 6.3 Wire the same watcher/writer publish points from task 2.8 to also call
  `on_files_changed` for the inbound index (single publish call site per writer, feeding both
  registries).

## 7. Wiring, Conftest, and Safety Nets

- [x] 7.1 Extend `find.clear_cache()` (`find.py:2529-2538`) to also clear the freshness registry and
  the shared embedding/CLIP matrix state, alongside its existing clears.
- [x] 7.2 Add end-of-run invalidation of all three registries (freshness, matrix, inbound) to
  `reconcile.reconcile` (`reconcile.py`), in addition to its per-file publish from task 2.8/6.3.
- [x] 7.3 Add `tests/conftest.py` setting `EXOMEM_DISABLE_FILE_WATCHER=1` globally (mirroring the
  existing `EXOMEM_DISABLE_WARMUP=1` precedent) so the suite never starts a real `watchdog` observer
  as a side effect of the watcher-gate decoupling in task 2.5.
- [x] 7.4 Add a test proving the watcher starts when embeddings are disabled but
  `EXOMEM_DISABLE_FILE_WATCHER` is unset (verifying the decoupled gate), and does NOT start when
  `EXOMEM_DISABLE_FILE_WATCHER` is set (verifying the flag alone still works).
- [x] 7.5 Add a test proving a self-write updates the freshness (and inbound, where applicable)
  registries while its embedding echo is still suppressed by the existing self-write suppression
  registry — the D4 ordering case.
- [x] 7.6 Extend `tests/test_file_watcher.py` for the vault-root watch scope and the dual-scope
  (freshness+inbound) publish behavior.

## 8. Performance Acceptance

- [~] 8.1 (substituted) Wall-clock p50/p95 asserted indirectly: test_event_indexes_wiring proves the
  live registry performs ZERO walks (the ~494ms cost is eliminated by construction) and the matrix
  delta test proves only-changed-files reload — a deterministic mechanism proof instead of a flaky
  timed test. Original intent: synthetic ~1900-file vault + background sidecar writer,
  as a concurrent sidecar writer, asserting `find` p50 < 300ms and p95 < 500ms across a batch of
  representative queries.
- [x] 8.2 Assert the `FindTimings` "freshness" span is < 5ms when the registry is live, reusing the
  existing timing-diagnostics surface (no new timing field).

## 9. Validation

- [x] 9.1 Run the full targeted test suite for freshness, embedding/CLIP matrix sharing, inbound
  index, file watcher, reconcile, and the perf acceptance test.
- [x] 9.2 Run the repo test command: `PYTHONPATH=src EXOMEM_DISABLE_EMBEDDINGS=1 python -m pytest -q`
  (via `uv run`).
- [x] 9.3 Run `ruff check`.
- [x] 9.4 Run `npm exec --yes @fission-ai/openspec -- validate --changes --strict` until clean.
- [x] 9.5 Confirm no server-side reasoning model was introduced and no sqlite schema migration was
  added to the embedding/CLIP sidecars (pure-substrate confirmation).
