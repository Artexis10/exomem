## Context

The boundary is one process-safe lock per vault. Interactive writers wait for it for a bounded time; background holders check `waiter_count()` between units and end their hold when it is positive. `foreground_priority` makes in-process bulk work yield to in-process foreground marks, with a progress floor so bulk work still finishes. The team's primary-source roster has no pick-by-problem row for lock priority, so this design follows the existing floors in `foreground_priority`; the replay marker follows "Writing effective tools for agents": a tool result says what actually happened.

## Decisions

### D1. An import apply yields at the chunk boundary and returns

The apply loop already lends the boundary to borrowers between chunks and stops on a request budget with a continuation. It now also stops when `writers_waiting()` is positive and the call has committed at least one chunk. The reply carries `stop_reason: interactive_write_waiting` and the continuation. The next call resumes from the cursor, so each item runs once.

Rejected: releasing the boundary in the middle of a call. It needs a per-run lease, or two applies interleave and duplicate items, and it needs plan and read revalidation on every re-entry. A return costs the agent one round trip per yield.

The one-chunk minimum is the progress floor: under a stream of writes, each call still commits a chunk.

### D2. A waiter counts only when it is interactive

`waiter_count()` counts threads of the process that wait with an interactive holder kind, plus the keyed writes queued behind the head of the capture line, plus a waiter in another process. Machine holder kinds and `maintenance-job` waiters are excluded, so background work does not yield to other background work. A borrower of a lent boundary waits for a loan and is excluded. The thread that polls the boundary's lock file raises the cross-process signal while it is interactive or while an interactive writer waits behind it in the process, re-evaluated at each poll, so a writer queued behind a local machine poller is still seen by a holder in another process.

The head ticket of the capture line stays in the line while its write runs, so the count starts behind the head. The head's own gaps between attempts are not visible to the count; its attempts already poll the boundary every 50 ms.

### D3. A hold that ended for a waiter hands the boundary over

A writer polls for the boundary every 25 ms. A holder that asks again at once wins the race. After a hold that ended while a writer waited, `hand_over()` waits while `waiter_count() > 0`, polling at the writer's rate, and returns at the cap of `foreground_priority.MAX_YIELD_SECONDS`, or earlier at the pass's progress share. A thread that holds a boundary never waits. A foreground thread does wait, because the wait is on a writer, not on itself, and the cap bounds it. A thread inside `ending_waits`, the collection store thread, skips the wait, because its waiter may need it.

The wait holds no lock. It can lengthen a job by at most the cap per hold; it cannot lengthen any writer's wait.

### D4. Reconcile phases that grow with the vault hold in batches

The unit-sidecar phase and the embedding phase ran one hold over every item. They now run through `apply_in_holds`: a hold takes batches of items and ends after `HOLD_SECONDS`, when a writer waits, or when a stop is requested. Each batch re-reads under the hold what it applies, as before. The phase hold labels are single tokens (`reconcile_units`), because the holder label sanitiser maps a label with a space to `unknown`.

The batches cost the job time. Every hold ends with a commit-generation bump, so readers re-validate their cached stamps, and a phase now bumps once per second of work, plus once per early end for a waiting writer, instead of once. The embedding phase refreshed every drifted page in one call; it now calls the refresh once per batch of 4 pages, so the fixed cost of each call repeats. The cost buys writer latency.

The other phases keep one hold. Their work is bounded by a constant or by seconds: the manifest, lifecycle cleanup, indexes, the graph build's own flight, derived-receipt batches (at most 256), references and receipts. They are reported as debt, not changed here.

### D5. Windows keeps a process-local signal

`msvcrt.locking` offers an exclusive lock only. A shared lock needs `LockFileEx` through `ctypes`, which this change cannot test. Windows holders see waiters in their own process, as before.

### D6. Replay marker and digest

`_replay` returns the stored terminal with `replayed: true`. `mutated`, `request_id` and `receipt_id` stay those of the original commit, so the reply names the write it replays. The marker leaves `mutated` alone because the egress filter binds the parent of a confined unit write on it. The implicit digest hashes the arguments with every string in Unicode NFC. FastMCP has already applied schema defaults to the arguments, so they need no second pass. The window stays 600 s, and the spec is corrected.

Skipped: `repeat_of`, a field that lets an agent repeat a write on purpose. An agent that repeats on purpose changes the arguments or waits out the window, and the field adds a schema entry to every write tool.

## Risks

- An import under a steady stream of writes returns one chunk per call. Each call makes progress, so the import finishes.
- The handoff wait delays a maintenance job by at most the cap per release, and only while a writer waits.
- A writer in another process that never gets served keeps the signal for its wait bound; the handoff wait ends at the cap.

## Known residuals

- Within 24 hours of the upgrade, a retry under an explicit key whose text is not in Unicode NFC gets `IDEMPOTENCY_KEY_REUSED`. The receipt stored before the upgrade holds a digest of the text as sent, and the new digest normalises it first. A retry under a new key commits.
- An import apply interrupted before the upgrade, with paths in NFD, gets `IMPORT_RECOVERY_UNRESOLVED` on resume, for the same reason: its stored request digest predates the normalisation.
