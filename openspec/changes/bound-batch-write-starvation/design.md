## Context

A media backfill (16-path `reprocess` calls and hour-long audio jobs) made foreground writes slow or refused on a personal service. The service log showed 176 commit-generation advances in 40 minutes, 78% from media. Each advance sets off corpus-sized derived work in the serving process, so request threads got little CPU. A separate worker process is out of scope. This design contains the load inside the one process.

## Decisions

### The graph checkpoint failed for two reasons, and both are fixed

1. The fan-out of a media publication was told that no page was created. A reprocess creates a prior-extraction page, so the checkpoint named a created page that the fan-out did not. The incremental refresh refused the checkpoint (`checkpoint_created_paths_mismatch`) and queued the paths instead. The fan-out now receives the checkpoint's created paths.
2. The graph floor and its checkpoint were published in a second boundary take. When that take was refused, the floor stayed ahead of the checkpoint. The next writer saw a recoverable epoch, wrote a full-scope checkpoint and rebuilt the whole graph (lineage gap). The epoch is now published inside the commit's own hold, so no refusable take sits between the floor and its checkpoint.

### One batch is one hold, one advance and one fan-out

The supervisor drains up to `BATCH_RESULT_LIMIT` durable results. It stages each result inside one hold, commits all their sidecar writes in one `batch_atomic_write`, and publishes the epoch in the same hold. The fan-out runs once after the hold, for all pages. A failure rolls the whole batch back. The results stay in `media_job_results`, so the supervisor publishes them again one at a time, and a result that cannot publish stops only itself. A result whose write paths overlap an earlier result in the batch waits for the next batch.

### Publication waits for a quiet foreground gap

The supervisor reuses `foreground_activity.idle_seconds`, the signal that the dreamer and the review pass use, and invents no second one. A batch needs `QUIET_GAP_SECONDS` of quiet, and `MIN_BATCH_SPACING_SECONDS` since the last batch. A result older than `MAX_DEFER_SECONDS` publishes without a quiet gap, so continuous traffic cannot hold publication back forever. The wait is durable and visible: the results stay in the job store, and `process_media status` reports their count and the age of the oldest.

All budgets live in `media_publication.py`. Both the supervisor and the status read use that module.

### A multi-path reprocess request queues under one hold

A reprocess path whose transcript is complete changes only the machine-local job store. The request queues such paths under one hold, until the hold has lasted `BATCH_HOLD_SECONDS`, then yields to waiting writes and takes the next hold. A path that needs a sidecar commit raises a signal before it writes. It runs afterwards with its own hold, so no index or graph fan-out runs inside the shared hold.

## Risks

- A media result can become visible a few minutes later than before under continuous foreground writes. The progress floor bounds the delay, and the status field shows it.
- A hold is longer than one unit's hold, by up to `BATCH_HOLD_SECONDS` plus one unit. A foreground write that waits for it is still inside its latency budget.
