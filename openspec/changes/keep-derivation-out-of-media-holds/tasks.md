## 1. Re-embed takes no boundary

- [x] 1.1 Extract the embedding arm of `index_sync._dispatch_upsert_components` into `index_sync.refresh_embeddings`, shared by the fan-out and the re-embed.
- [x] 1.2 `MediaWorker._run_reembed` takes no `mutation_guard`, keeps the recall-admission check before encoding, calls only `refresh_embeddings`, and keeps the transient `OpError` re-raise.

## 2. Scene frames leave the clip hold

- [x] 2.1 `_run_clip` commits the CLIP vectors under its guard and persists the scene frames after the guard is released.
- [x] 2.2 Split `scene_frames.write_scene_frames` into `prepare_scene_frames` (JPEG bytes, sidecar text, content guard; no hold), `commit_scene_frames` (one hold, `post_commit_fanout=False`, publication intents) and the synchronous wrapper; fan out after release through `_complete_deferred_graph_completion`.

## 3. Proof

- [x] 3.1 Replace the lock-placement tests with three guard tests: a sidecar edited during encoding leaves no rows from the old bytes; a recall exclusion that lands during encoding publishes no rows; scene frames are searchable after commit and no `background_media` hold is held when the encoder runs.
- [x] 3.2 Measure the holds before and after with one instrument: a keyed save during a 600-line re-embed, and the commit hold of a 4-, 40- and 80-frame video.

## 4. Gates

- [x] 4.1 Scoped tests (`test_media_worker.py`, `test_scene_frames.py`, `test_find_scene_frames.py`, `test_media_deletion_propagation.py`, `test_video_visual.py`, `test_index_sync.py`, `test_semantic_segments.py`) pass on the default interpreter (the Python 3.11 leg runs in CI); ruff, `git diff --check` and the public-artifact privacy gate pass.

## 5. Review

- [ ] 5.1 An independent adversarial review of the built change names each way a derived row can be stale, missing or published for withheld text.

## 6. Live acceptance and closure

- [ ] 6.1 After the release is live, a keyed save during a long transcript re-embed and during a multi-frame video job commits with no `MUTATION_BUSY`; the service's hold diagnostics show no `background_media_reembed_commit` hold and a `background_media_scene_frame_commit` hold bounded by the batch write; on an exact-v4 vault the hold's catalog-preparation share is recorded.
- [ ] 6.2 Run `openspec validate --all --strict`, then `openspec archive keep-derivation-out-of-media-holds`, in the delivery that closes the change.
