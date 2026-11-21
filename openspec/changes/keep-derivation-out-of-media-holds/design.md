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

The commit hold is the batch write of the frame files, and it is not constant. Measured after the derivation moved out, with zero encoder delay, `background_media_scene_frame_commit` held 0.87 s for 4 frames, 12.4 s for 40 frames and 23.0 s for 80 frames. An earlier run on the same instrument measured 7.4 s for the 40-frame case, so the commit's cost also moves with disk and CPU load. The measured saving of moving the derivation out is the work that real encoders add (frame image encoding, text-embedding encodes of every frame page, index fan-out), which the stub delay models and the zero-delay runs do not.

A profile traced most of the remaining hold to the batch writer's access admission. Before each flip it admitted every write that remained (`_validate_batch_write_access` over `writes[index:]`), so a batch of N writes ran about N²/2 write admissions. A 40-frame commit made 85 calls at 9.6 s cumulative under the profiler.

#### A batch admits each write once per stage

The batch writer now admits each write three times: with the whole batch before staging, with the whole batch before the first flip, and alone immediately before its own flip. Every check still runs for every write, inside the boundary and against the live access state:

- A refusal before the first flip still installs nothing, and a later refusal still rolls back the whole batch.
- Each check of a write reads the write itself, the policy and host state, or the write's own pre-image. A batch writes each path once, so each such input has one value before its flip and one after it.
- A check that reads one such input sees, at any flip between the first and the write's own, the value that one of those two admissions sees. The dropped admission therefore refuses only where one of them refuses, with the same error code.
- A check that reads two inputs that the same batch changes at different flips could see a mix that neither admission sees. Only an owner batch that also rewrites a policy file can do that, because no confined writer may write policy. The own-flip admission still checks the write against the state it is installed into.
- A foreign change that lands and reverts between two flips no longer refuses a write whose own flip comes later.

The map of planned writes by path, which only a confined writer's or capture-namespace admission reads, is now built on its first lookup. An unconfined writer no longer resolves the whole plan once per admitted write.

Hold seconds at zero delay, with the holding thread's CPU seconds in brackets. The figures come from two interleaved runs per arm on the same instrument, on a machine under load from other work (load average 22 to 24 on 16 cores):

| Frames | Before, run 1 | Before, run 2 | After, run 1 | After, run 2 |
|---|---|---|---|---|
| 4 | 1.54 [0.62] | 1.19 [0.64] | 1.55 [0.51] | 1.06 [0.58] |
| 40 | 8.30 [5.39] | 10.83 [6.60] | 7.19 [2.39] | 4.83 [3.13] |
| 80 | 25.53 [20.10] | 27.26 [22.42] | 15.70 [6.37] | 14.06 [8.59] |

An earlier run of the new code under lighter load held 0.97 s, 3.81 s and 10.6 s.

#### What still grows with the frame count

The hold still grows faster than the frame count, and it is well above 2 s for 40 frames. The remaining cost is the guard rechecks. At every flip the writer re-verifies the workspace census and every installed and pending artifact, so their work also grows with N². At 80 frames, timing wrappers without the profiler measured 3.2 s in the workspace census of an 8.6 s hold, against 0.47 s for 172 admissions. Each staged file is also fsynced once (163 calls and 1.4 s at 80 frames under the profiler), which grows linearly.

Follow-up, as a separate change: a recheck schedule that stays linear in the batch size. It changes how soon the writer detects a foreign change to a file that it installed or staged, so it needs its own design and proof. Committing the frames in several bounded batches would also bound the hold, but it gives up the one rollback set that binds an exact-v4 vault's catalog successor to the frame set, so it needs its own design.

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
