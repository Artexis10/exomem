## Context

The media worker takes the vault mutation boundary for each durable result it commits. Two holds were long. Both ran work that does not need the boundary: model inference and image encoding. A third kind of work, the index fan-out after a sidecar write, already runs after the hold for sidecar commits (`Background media commit refreshes derived indexes`). The scene-frame persistence was the exception, and the re-embed had never been classified.

Closest existing mechanism, and why this change reuses it: the media publication batch (`bound-batch-write-starvation`) already commits under one hold and completes its fan-out after release through `DeferredGraphCompletion`, `publication_intents_out` and `_complete_deferred_graph_completion`. This change routes scene frames through that path and adds no second one.

## Decisions

### A re-embed takes no boundary

`_run_reembed` called `index_sync.upsert_after_write` inside a `mutation_guard`. The embedding arm of that call is the only step that matters for a re-embed, because the worker enqueues a re-embed after it has changed the sidecar (frame OCR, segmentation) and the page's other derived state was already published by that commit.

`index_sync.refresh_embeddings` is that arm, extracted from `_dispatch_upsert_components` and shared by it. The re-embed calls it with no guard.

The boundary is not needed because the embedding store publishes under its own write transaction, and the transaction's validator runs inside it:

- `still_current()` re-checks recall admission, the sidecar's `stat_signature` and the recall policy identity captured before encoding. It runs before encoding starts and again inside `EmbeddingIndex.upsert_batch`'s validate callback.
- A sidecar that moved while it encoded fails the second check. The store purges the sidecar's rows and records a durable deferred semantic upsert, so a later pass embeds the new bytes. No row from the old bytes remains.
- A sidecar withheld from recall while it encoded (a `Knowledge Base/_access.yaml` exclusion) fails the same check and publishes nothing.

Cases of a derived row that could be stale, missing or published for withheld text:

| Case | Outcome |
|---|---|
| Sidecar edited during encoding | Validator rejects, rows purged, deferred upsert recorded. |
| Recall admission lost during encoding | Validator rejects, rows purged, deferred upsert recorded. |
| Recall policy identity changed during encoding | Validator rejects, as above. |
| Sidecar already withheld before encoding | The pre-encode check returns before any encode. |
| Process stops between encode and publish | Nothing published. The queued job or the periodic scan runs the re-embed again. |

A transient `OpError` (busy boundary, deferred semantic work) still re-raises so the job retries with backoff, as before.

### Scene frames persist after the clip hold, and commit in one hold

`_run_clip` took the scene-frame persistence inside `with clip_guard:`, so one hold covered the CLIP vectors, the JPEG encode of every frame, the frame-page writes and their fan-out. Now:

1. The clip hold commits the CLIP vectors only.
2. `prepare_scene_frames` runs with no hold. It encodes the JPEG bytes, renders the sidecar text, hashes the video for its content guard and builds the planned writes.
3. `commit_scene_frames` runs under a hold named `background_media_scene_frame_commit`. It adds the full-index receipts for the frame sidecars, commits the batch with `post_commit_fanout=False` and registers the watcher self-writes as publication intents, then publishes the graph epoch in the same hold.
4. The fan-out of the new pages runs after the hold through `_complete_deferred_graph_completion`.

Crash between step 3 and step 4: the receipts added before the commit mark the frame sidecars as owed a full index. The drain replays the full upsert for them, and `recover_full_receipt_graph_epoch` recovers the graph epoch. This is the same repair that covers a crash after any media sidecar commit.

A reprocess replaces an earlier frame set. `clear_scene_frames` removes the old frames and purges their rows in the hold (`delete_after_remove`), so the removal precedes the new frames' fan-out. This purge runs no model and stays in the hold. A crash after the unlink and before the purge leaves stale rows that the watcher and reconcile repair as before. Exact-v4 vaults refuse a replacement of an existing frame set already, and that refusal is unchanged.

### Honest limit: the frame commit still grows with the frame count

The commit hold is the batch write of the frame files, and it is not constant. Measured after this change with zero encoder delay, `background_media_scene_frame_commit` holds 0.87 s for 4 frames, 12.4 s for 40 frames and 23.0 s for 80 frames, which is about 0.3 s per frame (one image file and one page per frame). An earlier run on the same instrument measured 7.4 s for the 40-frame case, so the commit's cost also moves with disk and CPU load. The cost is the batch writer: it calls `_validate_batch_write_access` once per write over the writes that remain, and it fsyncs each write. A profile of a 40-frame commit on the shipped code shows 83 calls to `_validate_batch_write_access` at 9.4 s cumulative under the profiler.

This change does not alter `batch_atomic_write`. A change to its per-write revalidation touches the checks that protect every batch write, so it needs its own design and proof. This change therefore states the guarantee it delivers (no model work, no image encoding and no index fan-out inside the hold) and does not claim that the hold is independent of the frame count. The measured saving of this change for scene frames is the work that real encoders add (frame image encoding, text-embedding encodes of every frame page, index fan-out), which the stub delay models and the zero-delay runs do not. At zero delay the hold of the shipped code and the new commit hold are close, because the batch commit dominates both.

Follow-up, as a separate change: validate the remaining writes of a batch once instead of once per write, and measure the fsync share. Committing the frames in several bounded batches would also bound the hold, but it gives up the one rollback set that binds an exact-v4 vault's catalog successor to the frame set, so it needs its own design.

The spec requirement carries the same limit: it bounds the hold by the sidecar's length and keeps derived computation out of it, and it makes no claim about the frame count.

Exact-v4 vaults add catalog preparation (`prepare_planned_markdown_batch` and the corpus context it builds) to the commit hold. The proof runs on a vault that is not exact-v4, so that cost is unmeasured here. The live acceptance task measures it.

## Proof

Instrument: a real `MediaWorker` runs a real job on a vault in temporary state. A stub replaces the text encoder (`embeddings.embed_texts`) with a fixed delay per call and, for scene frames, the image encoder with a fixed delay per frame. A recorder logs every outermost vault boundary hold with its duration and holder kind. For the re-embed, a keyed `remember` runs while the job encodes and its wait is timed. The shipped code runs from a copy of the base revision's `src` placed first on `PYTHONPATH`. The same test file runs against both.

Re-embed (600-line timed sidecar, 0.05 s per encode call, a job of about 30 s):

- Shipped code: the keyed save waited 29.52 s, was refused with an operation error and saved nothing; `background_media_reembed_commit` held 30.65 s.
- This change: the keyed save waited 1.60 s and committed; no `background_media_reembed_commit` hold exists. The job still takes 30.3 s, outside any hold. A save into an idle vault takes 3.6 s on the same instrument, so the 1.60 s wait is the save's own cost and not a wait for the job.

Scene frames, 40 frames, 0.05 s delay per text encode and per frame image encode:

- Shipped code: one `background_media_clip_commit` hold of 24.3 s.
- This change: `background_media_clip_commit` holds 0.10 s and `background_media_scene_frame_commit` holds 12.1 s, of which the batch commit is nearly all (see the limit above).

Scene frames at zero delay (hold seconds; shipped code: one clip hold; this change: the frame-commit hold):

| Frames | Shipped code | This change |
|---|---|---|
| 4 | 1.88 | 0.87 |
| 40 | 13.29 | 12.39 |
| 80 | 43.81 | 22.98 |

## Risks

- A re-embed publishes no corpus, custody, memory-reference, lexical or graph state. The sidecar commit that precedes the queued re-embed already published that state with its own fan-out. A future caller that edits a sidecar and then asks only for a re-embed would lose it. The re-embed's only caller is the worker's queue entry, which follows a sidecar commit.
- Without the boundary, a re-embed and a foreground edit of the same sidecar can overlap. The store's validator resolves the overlap: the edit wins, and the re-embed's rows are purged and re-queued.
- A frame commit hold of several seconds for a long video is still a hold. The follow-up change addresses it.
