## Context

A media backfill (16-path `reprocess` calls and hour-long audio jobs) made foreground writes slow or refused on a personal service. The service log showed 176 commit-generation advances in 40 minutes, 78% from media. Each advance sets off corpus-sized derived work in the serving process, so request threads got little CPU. A separate worker process is out of scope. This design contains the load inside the one process.

## Decisions

### The graph checkpoint failed for two reasons, and both are fixed

1. The fan-out of a media publication was told that no page was created. A reprocess creates a prior-extraction page, so the checkpoint named a created page that the fan-out did not. The incremental refresh refused the checkpoint (`checkpoint_created_paths_mismatch`) and queued the paths instead. The fan-out now receives the checkpoint's created paths.
2. The graph floor and its checkpoint were published in a second boundary take. When that take was refused, the floor stayed ahead of the checkpoint. The next writer saw a recoverable epoch, wrote a full-scope checkpoint and rebuilt the whole graph (lineage gap). The epoch is now published inside the commit's own hold, so no refusable take sits between the floor and its checkpoint.

### One batch is one hold, one advance and one fan-out

The supervisor drains up to `BATCH_RESULT_LIMIT` durable results. It stages each result inside one hold, commits all their sidecar writes in one `batch_atomic_write`, and publishes the epoch in the same hold. The fan-out runs once after the hold, for all pages. A failure rolls the whole batch back and leaves the results in `media_job_results`. The next batch is half the size, until a result that cannot publish stands alone; that result is set aside and retried alone once it has waited `MAX_DEFER_SECONDS`, so the others publish meanwhile. Isolating a failing result costs one failed hold a pass, never a burst of holds. A result that fails while it is staged names itself: the batch sets it aside and commits the others in the same hold. A refused boundary ran nothing, so the batch stays pending, whole, for the next pass. A result whose write paths overlap an earlier result in the batch waits for the next batch.

### Publication waits for a quiet foreground gap

The supervisor reuses `foreground_activity.idle_seconds`, the signal that the dreamer and the review pass use, and invents no second one. A batch needs `QUIET_GAP_SECONDS` of quiet, and `MIN_BATCH_SPACING_SECONDS` since the last hold. Every hold that ran sets the spacing, because every hold advances the commit generation, even one that staged nothing or rolled back. The progress floor lets a result publish without a quiet gap once `MAX_DEFER_SECONDS` have passed, so continuous traffic cannot hold publication back forever. The floor counts from the later of the oldest result and the last hold, so a result that never stages opens it once per floor. While foreground writes stay busy the supervisor reads the queue once per floor, and polls only the clock and the activity signal between. The wait is durable and visible: the results stay in the job store, and `process_media status` reports their count and the age of the oldest. The count is unknown (`null`) when the read fails, and zero only when the results table does not exist.

All budgets live in `media_publication.py`. The supervisor's gate and its batch publisher read that module; the status read reports the queue they act on and uses no budget.

### A reprocess request queues without advancing the commit generation

Planning a reprocess path reads the whole file for its hash, and for an evicted original it hydrates the file first. That work never runs under the boundary, because one large file would stall every foreground write. A path whose transcript is complete then commits only a row in the machine-local job store. The request takes that row's hold with `projection_guard`, which holds the boundary and leaves the commit generation alone, so six paths advance it zero times where each used to advance it twice. A path that needs a sidecar commit takes the ordinary advancing hold, and its fan-out runs after the hold. A non-advancing hold never writes canonical content: if the media runtime becomes unavailable between the plan and the hold, the path reports `MEDIA_RUNTIME_UNAVAILABLE` and the caller asks again.

## Risks

- A media result can become visible a few minutes later than before under continuous foreground writes. The progress floor bounds the delay, and the status field shows it.
- A publication hold lasts up to `BATCH_HOLD_SECONDS` plus the staging of one result, because the time cap is checked between results. A reprocess queueing hold covers one job row, with no planning inside it. A foreground write that waits for either is inside its latency budget.
