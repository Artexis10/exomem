## 1. Re-embed takes no boundary

- [x] 1.1 Extract the embedding arm of `index_sync._dispatch_upsert_components` into `index_sync.refresh_embeddings`, shared by the fan-out and the re-embed.
- [x] 1.2 `MediaWorker._run_reembed` takes no `mutation_guard`, keeps the recall-admission check before encoding, calls only `refresh_embeddings`, and keeps the transient `OpError` re-raise.

## 2. Scene frames leave the clip hold

- [x] 2.1 `_run_clip` commits the CLIP vectors under its guard and persists the scene frames after the guard is released.
- [x] 2.2 Split `scene_frames.write_scene_frames` into `prepare_scene_frames` (JPEG bytes, sidecar text, content guard; no hold), `commit_scene_frames` (one hold, `post_commit_fanout=False`, publication intents) and the synchronous wrapper; fan out after release through `_complete_deferred_graph_completion`.
- [x] 2.3 `_batch_atomic_write_locked` admits the whole batch before its first flip and each later write alone before its own flip, instead of every remaining write at every flip; the plan-by-path map that admission passes to `connector_boundary.require_write` is built on its first lookup.

## 3. Proof

- [x] 3.1 Replace the lock-placement tests with three guard tests: a sidecar edited during encoding leaves no rows from the old bytes; a recall exclusion that lands during encoding publishes no rows; scene frames are searchable after commit and no `background_media` hold is held when the encoder runs.
- [x] 3.2 Measure the holds before and after with one instrument: a keyed save during a 600-line re-embed, and the commit hold of a 4-, 40- and 80-frame video.
- [x] 3.3 A test bounds the access admissions of each write in a 40-write batch, which the old schedule exceeds 14 times over; the batch admission schedule is measured at 4, 40 and 80 frames on the same instrument.

## 4. Gates

- [x] 4.1 Scoped tests (`test_media_worker.py`, `test_scene_frames.py`, `test_find_scene_frames.py`, `test_media_deletion_propagation.py`, `test_video_visual.py`, `test_index_sync.py`, `test_semantic_segments.py`) pass on the default interpreter (the Python 3.11 leg runs in CI); ruff, `git diff --check` and the public-artifact privacy gate pass.
- [x] 4.2 Scoped tests for the batch admission schedule (`test_vault.py`, `test_batch_payload_custody.py`, `test_scene_frames.py`, `test_transactional_writes.py`, `test_confined_writers.py`, `test_connector_boundary.py`, `test_compiled_history.py`, `test_lifecycle_admission_boundaries.py`, `test_access.py`, `test_capture_boundary_hold.py`, `test_narrow_boundary_leaf_guards.py`, `test_media_publication_batch.py`, `test_record_mutation.py`, and the batch and publication subsets of `test_collection_store_service.py`, `test_vault_store_write_path.py` and `test_media_worker.py`) pass on the default interpreter.

## 5. Review

- [ ] 5.1 An independent adversarial review of the built change names each way a derived row can be stale, missing or published for withheld text.

## 6. Live acceptance and closure

- [ ] 6.1 After the release is live, a keyed save during a long transcript re-embed and during a multi-frame video job commits with no `MUTATION_BUSY`; the service's hold diagnostics show no `background_media_reembed_commit` hold and a `background_media_scene_frame_commit` hold bounded by the batch write; on an exact-v4 vault the hold's catalog-preparation share is recorded.
- [ ] 6.2 Run `openspec validate --all --strict`, then `openspec archive keep-derivation-out-of-media-holds`, in the delivery that closes the change.
