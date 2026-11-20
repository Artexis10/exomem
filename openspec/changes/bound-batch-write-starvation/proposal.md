## Why

A foreground keyed write was refused `MUTATION_BUSY` for minutes while an agent ran back-to-back `process_media` calls of 11–16 paths each (50–350 s per call). Three defects, seen in the service's own spans:

- A keyed write's place in the arrival-order queue lasted until its whole command returned. A multi-path command's ticket therefore parked every later write for the command's whole run, even while the vault boundary sat idle — edits timed out with `capture_wait_exhausted` having never run a span.
- The fast-receipt commit tail ran `register_self_write_and_close` while the caller still held the boundary. That tail publishes the corpus, inbound and resolver registries and can wait on an inner lock that a long bulk operation holds; every long `process_media` boundary hold measured equalled its `commit_tail.self_write` time (122.6 s of 121.9 s, 54.8 of 55.3, 31.3 of 35.9).
- A multi-path request and the background media worker re-took the boundary per unit with no deference to a waiting write, and `waiter_count` did not see writes parked in the queue behind the head.
- Media publication cost two boundary holds and two commit-generation advances per result, and a multi-path `reprocess` call two per path. Each advance sets off corpus-sized derived work in the serving process. A media backfill advanced the generation 176 times in 40 minutes, which starved foreground writes, and some publications triggered a full graph rebuild.

## What Changes

- A keyed write's queue turn ends when its attempt holds the vault mutation boundary, not when its command returns. A busy refusal after the acquisition re-queues the write behind the waiters of the moment. On the store route the store thread's acquisition counts, because the attempt runs in a copy of the request's context.
- Between two units, a multi-path media request and the background media worker wait — at most a bounded wait per unit — while a keyed write waits in line or at the boundary. Batch work slows; it never starves a writer.
- A commit under the boundary publishes its derived registries for at most a short fixed bound. Past the bound the boundary releases and a single worker completes the publication in commit order, closing the commit's publication intents itself. The boundary-held attempt never runs a cold whole-vault corpus build.
- The media supervisor publishes the results it drains as one batch: one boundary hold, one commit-generation advance and one post-commit fan-out for the whole batch. The batch lands whole or rolls back, and the batches after a rolled-back one shrink until the failing result stands alone and is set aside.
- The graph epoch (floor and checkpoint) is published inside the commit's own hold, and the post-commit fan-out names the pages the commit created. A media publication therefore completes the graph incrementally and never schedules a full rebuild.
- The supervisor takes the boundary for a batch only in a quiet gap of foreground writes, at most once a minute, and a result that has waited past a progress floor publishes anyway. `process_media status` shows the pending publication count and the oldest age.
- A `reprocess` request plans each path outside the boundary and queues a path whose transcript is complete under a hold that does not advance the commit generation, so a multi-path request no longer advances it twice for each path.
- Two races the ended turn surfaces now fail safe instead of failing the write: a raw-lane walk that meets a vanished staged directory treats it as gone rather than incomplete, and a planned auxiliary another writer moved before the pre-boundary prepare reports the same retryable busy the in-boundary commit already reports.

## Capabilities

### Modified Capabilities

- `hosted-mutation-safety`: the queue turn ends at boundary acquisition; multi-unit writers give way between units; the commit tail stays inside a short fixed bound; media publication lands in whole bounded batches, in quiet gaps, with an incremental graph.

## Impact

- Code: `writer_lease.py`, `mutation_lock.py`, `file_watcher.py`, `commands.py`, `media_worker.py`, `media_publication.py`, `media_jobs.py`, `media_processing.py`, `source_capture.py`, `relation_review.py`.
- Behaviour: a long batch no longer parks later keyed writes; a blocked registry publication no longer lengthens a boundary hold; a write whose plan went stale to a concurrent commit retries instead of failing with `PATH_GUARD_CHANGED`; a media result becomes visible up to a few minutes later under continuous foreground writes, and `process_media status` reports that wait in a new `result_publication` field.
- Undo: revert the change. No stored file format changes.
