# Proposal: fix-media-deletion-propagation

## Why

Deleting a page fans out to every derived index through
`index_sync.delete_after_remove` (`:606`) — lexical/BM25, embedding chunks,
semantic-unit vectors, graph nodes/edges, memory refs, resolver/freshness caches
all purge correctly. Two media-derived artifacts do not:

- **CLIP image/frame vectors survive.** `ClipIndex.delete` exists
  (`clip_index.py:180`) but is called from no deletion path — only from backfill,
  find candidates, the media worker, and warmup. A deleted image or video leaves
  stale visual-search rows that still return the removed content by `clip_score`.
- **Scene-frame `.frames/` derivatives survive a single-file delete.**
  `scene_frames.clear_scene_frames` (`:59`) runs only on re-processing, not on
  deletion. Deleting a video's `.md` sidecar orphans the sibling `<video>.frames/`
  directory and its per-frame CLIP rows.

Worse, `delete_file` gates the entire fan-out on `rel_path.lower().endswith(".md")`
(`delete_file.py:254`), so deleting a media **binary** triggers no fan-out at all.

This is a correctness/right-to-erasure defect on its own, and a hard prerequisite
for the governance deletion story (a governed source must leave no visually
searchable residue). It is deliberately scoped as a standalone fix so it can land
independently of the governance kernel.

## What Changes

- `index_sync.delete_after_remove` gains a `clip` component that purges the CLIP
  rows for the removed path and, for a video, its `<video>.frames/` children.
- The fan-out also invokes `scene_frames.clear_scene_frames` for a removed video
  so frame sidecars and the `.frames/` directory are cleaned.
- `delete_file`'s `.md`-only fan-out gate widens: a removed **media binary**
  (detected via `media_types`) enters the fan-out with its frame children,
  instead of silently skipping index cleanup.
- `reconcile` heals pre-existing orphans (stale CLIP rows, dangling `.frames/`)
  so vaults that already lost content are repaired, not just future deletes.

## Capabilities

### Modified Capabilities

- `hosted-vault-portability`: the deletion propagation contract this capability
  relies on for quiesced export/delete now covers CLIP and scene-frame
  derivatives, so a deleted artifact leaves no searchable residue in any sidecar.

(The core deletion behavior is not otherwise a named capability; this change is
primarily an implementation-correctness fix asserted by tests.)

## Impact

- Code: `src/exomem/index_sync.py` (new `clip` fan-out component),
  `src/exomem/delete_file.py` (media-binary fan-out gate),
  `src/exomem/embeddings.py` (a clip-delete status helper mirroring
  `delete_after_remove_status`), `src/exomem/scene_frames.py` (no-op-if-absent
  guard), `src/exomem/reconcile.py` (orphan healing).
- Tests: new `tests/test_media_deletion_propagation.py`.
- No new dependencies; no schema changes.
- Explicitly NOT in scope: governance, retention policy, deletion **events**
  (those land in `add-disclosure-receipts`). This change only closes the purge
  gap.
