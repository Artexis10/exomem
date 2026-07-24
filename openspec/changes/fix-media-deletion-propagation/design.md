# Design: fix-media-deletion-propagation

## Context

`index_sync.delete_after_remove(vault_root, rel_paths)` (`:606`) is the single
deletion fan-out, called by `delete_file` (`:257`) and `delete_directory`
(`:237`). It dispatches to lexstore, memory_refs, epistemic_graph, and
`embeddings.delete_after_remove_status` (`embeddings.py:1275`, text
`EmbeddingIndex.delete_file` only). `ClipIndex.delete` (`clip_index.py:180`,
does `dual_delete` + `DELETE FROM images`) and `scene_frames.clear_scene_frames`
(`:59`) both exist but are unreachable from deletion. The binaries and frame
sidecars are `Evidence`/append-only originals — but their **derived index rows**
are rebuildable sidecar state that should track deletion.

## Goals / Non-Goals

Goals: a deleted image/video/frame leaves no CLIP row and no orphan `.frames/`
directory; deleting a media binary triggers index cleanup; a reconcile pass
heals vaults that already have orphans.

Non-Goals: deleting append-only originals themselves (unchanged — trash
semantics only), governance/retention policy, deletion audit events (deferred to
`add-disclosure-receipts`), any change to how CLIP rows are *created*.

## Decisions

### D1 — CLIP purge as a fan-out component, best-effort
Add a `clip` component to `delete_after_remove` (`:620-632` component list)
calling a new `embeddings.delete_clip_after_remove(vault_root, rels)` that resolves
the CLIP index and calls `ClipIndex.delete` for the removed path and each
`<video>.frames/` child. Wrap in the same `_legacy_component` best-effort guard
the other components use, so a missing CLIP extra (soft-fail) never breaks a
delete. Rationale: keeps CLIP consistent with the other four sidecars, single
fan-out point, no new call sites to keep in sync.

### D2 — Scene-frame cleanup on the same fan-out
For a removed video, the fan-out also calls
`scene_frames.clear_scene_frames(vault_root, video_abs)` and passes the frame
sidecar `.md` rels through the existing lexical/embedding components (they are
`.md` paths — expand them into the fan-out's path set). Guard no-ops when no
`.frames/` exists.

### D3 — Widen the delete_file fan-out gate for media binaries
`delete_file.py:254` currently gates fan-out on `.md`. Change: if the removed
path is a media binary (by extension via `media_types`), call
`delete_after_remove` with `[rel_binary] + frame_children`; non-md non-media
keeps current behavior (no fan-out needed). This closes the "delete the binary,
orphan every derived row" hole.

### D4 — Reconcile heals existing orphans
The existing `reconcile` sweep that prunes stale embeddings gains the same CLIP +
`.frames/` orphan detection, so a vault that already lost content through the gap
is repaired idempotently — not only future deletes. Drift is seeded manually in
the test.

## Risks / Trade-offs

- **CLIP extra absent (lean install)**: `get_clip_index` soft-fails; the
  best-effort component wrapper must swallow that and not fail the delete. Test
  both with and without the extra.
- **Frame directory races the watcher**: `file_watcher` may observe the `.frames/`
  removal; `register_self_delete` already suppresses the writer's own events —
  extend the suppression to the frame children to avoid a re-index echo.
- **Idempotency**: deleting an already-clean path (no CLIP rows) must be a no-op,
  not an error — asserted.

## Migration Plan

Additive fan-out + a reconcile heal. No data migration; the reconcile pass is the
migration for already-orphaned vaults and is safe to run repeatedly.

## Open Questions

None blocking.
