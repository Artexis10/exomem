## 1. Queue turn ends at boundary acquisition

- [x] 1.1 Add a thread-safe `_ContentionWait.release_turn()` that leaves the line and releases the counted waiter slot; make `close()` idempotent with it.
- [x] 1.2 Add `mutation_lock.on_boundary_acquired(callback)`, fired once on a depth 0→1 vault-boundary acquisition on the calling thread or on a store thread running the attempt in a copied context; never on a re-entrant, borrowed or reserved-identity hold; observer exceptions never break the acquire.
- [x] 1.3 Wire `wait.release_turn` around the attempt in `_absorb_contention`; a later absorbable refusal re-queues at the back of the line.

## 2. Batches give way between units

- [x] 2.1 Add `LeaseManager.yield_to_waiting_writes(vault_root, max_wait=30.0)`: no-op under a held boundary; waits while a write waits in the capture line or at the boundary; records `batch.yield_to_writes`.
- [x] 2.2 Call it before every `process_media` path after the first and in `MediaWorker._holding_boundary` before the guard.
- [x] 2.3 Teach the media-worker recording managers the new method.

## 3. Time-limited commit tail

- [x] 3.1 Route `register_self_write_and_close` to a single module-level worker (`exomem-self-write-publish`, copied context) when the calling thread holds the boundary; wait at most `_IN_BOUNDARY_PUBLICATION_WAIT_SECONDS`, then return False and hand the intents to the worker, which finalizes or aborts them itself.
- [x] 3.2 Pass `populate_on_miss=False` through the handed-off publication so no cold whole-vault corpus build runs as boundary work; keep inline behaviour when no boundary is held.
- [x] 3.3 Add `commit_tail.self_write.corpus` / `.inbound` / `.resolver` / `.wait` spans.

## 4. Races the ended turn surfaces

- [x] 4.1 A staged directory that vanishes mid-walk counts as gone, not as an incomplete raw enumeration.
- [x] 4.2 A planned auxiliary another writer moved before the pre-boundary prepare reports the retryable moved-write busy the in-boundary commit already reports.

## 5. Proof

- [x] 5.1 A keyed write that arrived while an earlier write ran commits before that command ends; a released ticket rejoins at the back after another refusal; the observer fires once per outer acquire and never for a reserved lock.
- [x] 5.2 A real multi-path `process_media reprocess` with slowed units lets a concurrent keyed `edit_memory` commit mid-batch, within about one unit's hold, and completes every path.
- [x] 5.3 With `_CORPUS_CONTEXT_UPDATE_LOCK` held, a sidecar commit's boundary stay stays inside the bound and the publication completes after release; the same holds with `_RESOLVER_LOCK`; two consecutive commits publish in commit order.
- [x] 5.4 Concurrent keyed `remember` calls all commit once with no busy reaching the client.
- [ ] 5.5 After the release is live, the personal service's refusal journal shows no `capture_wait_exhausted` while the boundary sat idle and no boundary hold that equals a `commit_tail.self_write` span.

## 6. Media publication containment

- [x] 6.1 Pass the checkpoint's created paths to the post-commit fan-out, and publish the graph floor and checkpoint inside the commit's own hold, so a media publication completes the graph incrementally.
- [x] 6.2 Publish drained media results as one batch (one hold, one commit, one advance, one fan-out) with a size cap and a time cap; a rolled-back batch is followed by smaller batches until the failing result is alone and set aside; a refused boundary leaves the batch pending.
- [x] 6.3 Gate batches on `foreground_activity.idle_seconds`, a minimum spacing that every hold that ran sets, and a progress floor counted from the later of the oldest result and the last hold; keep every budget in `media_publication.py`.
- [x] 6.4 Report `result_publication` (pending count, oldest age) in `process_media status`; the count is unknown when the read fails.
- [x] 6.5 Plan each `reprocess` path outside the boundary and queue a path whose transcript is complete under `projection_guard`, which does not advance the commit generation; a path that needs a sidecar commit keeps its own advancing hold.
- [x] 6.6 Proof: a batch of results takes one hold and one advance and lands whole; a failed batch rolls back and shrinks until the failing result is set aside; a refused boundary costs one attempt; the gate defers under foreground activity and publishes at the floor; a reprocess that creates a page completes the graph without a fallback; six completed paths queue with no advance; no reprocess planning runs under the boundary.
- [ ] 6.7 After the release is live, the personal service's log shows media-originated commit-generation advances at about one a minute or fewer under a media backfill, and no full graph rebuild from a media publication.

## 7. Closure

- [ ] 7.1 Archive this change with `openspec archive` once tasks 1–6 are merged, and run `openspec validate --all --strict` before and after.
