# Tasks: fix-media-deletion-propagation

## 1. CLIP purge in the fan-out

- [x] 1.1 Red test `tests/test_media_deletion_propagation.py::test_delete_after_remove_drops_clip_rows`:
      insert rows via `ClipIndex.upsert`, run `delete_after_remove`, assert rows
      gone and the CLIP generation token bumped.
- [x] 1.2 Add `embeddings.delete_clip_after_remove(vault_root, rels)` mirroring
      `delete_after_remove_status`; wire a best-effort `clip` component into
      `index_sync.delete_after_remove` (`:620-632`), guarded by `_legacy_component`.
- [x] 1.3 Assert no-op when the CLIP extra is absent (lean install) — delete still
      succeeds.

## 2. Scene-frame cleanup

- [x] 2.1 Red test `test_delete_after_remove_clears_scene_frames`.
- [x] 2.2 Fan-out calls `scene_frames.clear_scene_frames(root, video_abs)` for a
      removed video and routes frame sidecar `.md` rels through the existing
      lexical/embedding components; no-op guard when no `.frames/` exists.

## 3. Media-binary fan-out gate

- [x] 3.1 Red test `test_delete_file_media_binary_enters_fanout`.
- [x] 3.2 Widen `delete_file.py:254`: a removed media binary (via `media_types`)
      calls `delete_after_remove([rel_binary] + frame_children)`; extend
      `register_self_delete` suppression to the frame children.

## 4. Reconcile healing

- [x] 4.1 Red test `test_reconcile_heals_clip_and_frame_orphans` (seed drift
      manually; assert idempotent).
- [x] 4.2 Add CLIP + `.frames/` orphan detection to the `reconcile` sweep that
      already prunes embeddings.

## 5. Gates

- [x] 5.1 `PYTHONPATH=src EXOMEM_DISABLE_EMBEDDINGS=1 uv run python -m pytest -q
      tests/test_media_deletion_propagation.py tests/test_index_sync.py
      tests/test_scene_frames.py` green.
- [x] 5.2 `uv run python -m pytest tests/test_latency_gate.py -q` green.
- [x] 5.3 `uvx ruff check` clean on changed files; `openspec validate
      fix-media-deletion-propagation --strict` green.
