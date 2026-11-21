## Why

A keyed note save waited behind a background media job and was refused `MUTATION_BUSY`. The operator's requirement: a media ingest must never block a session, because a refused save is a lost save.

Two background media boundary holds ran model work or per-frame work while they held the vault mutation boundary:

- `background_media_reembed_commit` ran the whole embedding refresh of a transcribed sidecar under the boundary. The refresh encodes every segment of the sidecar with the text model, so the hold grows with the sidecar's length. A re-embed changes no canonical byte, so the boundary protected nothing.
- `background_media_clip_commit` also held the boundary for the scene-frame persistence. That step encoded each frame to JPEG, wrote every frame page and fanned each page out to the indexes (including the text-embedding encode of each page) inside the hold.

On the shipped code a 600-line timed sidecar held the boundary for 30.65 s, and a keyed save that arrived during it waited 29.52 s and was refused. A 40-frame video with a 0.05 s encoder delay held the boundary for 24.3 s.

## What Changes

- A re-embed takes no vault boundary. It refreshes the embedding rows through the store's own write transaction. That transaction re-checks the sidecar's file signature, its recall admission and the recall policy identity it read, and publishes nothing when any of them moved. A re-embed of a sidecar that is no longer admitted for recall encodes nothing.
- Scene-frame persistence runs after the clip commit hold. It prepares the JPEG bytes and sidecar text before any boundary, commits the frame files in one hold, and fans the pages out to the indexes after the hold is released, through the deferred graph-completion path that the media publication batch already uses.
- A crash between the frame commit and the fan-out leaves durable full-index receipts, which the existing drain replays. The change adds no new repair path.

## Capabilities

### Modified Capabilities

- `hosted-mutation-safety`: "Media Work Uses Bounded Per-Artifact Commit Guards" drops its carve-out for CLIP-vector commits and scene-frame persistence, and states that background media work computes no derived result inside a boundary hold.

## Impact

- Code: `index_sync.py` (one function extracted, `refresh_embeddings`), `media_worker.py`, `scene_frames.py`.
- Behaviour: a note save during a re-embed or a scene-frame job no longer waits for the job's model work. A re-embed no longer re-runs the exact-path custody, memory-reference, lexical and graph steps of a full upsert, because it changes no canonical byte; it refreshes embeddings only.
- Known limit: the frame-page batch commit still runs inside one hold, and its cost grows with the frame count. See the design.
- Undo: revert the change. No stored file format changes.
