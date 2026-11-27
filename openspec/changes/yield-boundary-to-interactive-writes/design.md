## Context

The boundary is one process-safe lock per vault. Interactive writers wait for it for a bounded time; background holders check `waiter_count()` between units and end their hold when it is positive. `foreground_priority` makes in-process bulk work yield to in-process foreground marks, with a progress floor so bulk work still finishes. The team's primary-source roster has no pick-by-problem row for lock priority, so this design follows the existing floors in `foreground_priority`; the replay marker follows "Writing effective tools for agents": a tool result says what actually happened.

## Decisions

### D1. An import apply yields at the chunk boundary and returns

The apply loop already lends the boundary to borrowers between chunks and stops on a request budget with a continuation. It now also stops when `writers_waiting()` is positive and the call has committed at least one chunk. The reply carries `stop_reason: interactive_write_waiting` and the continuation. The next call resumes from the cursor, so each item runs once.

Rejected: releasing the boundary in the middle of a call. It needs a per-run lease, or two applies interleave and duplicate items, and it needs plan and read revalidation on every re-entry. A return costs the agent one round trip per yield.

The one-chunk minimum is the progress floor: under a stream of writes, each call still commits a chunk.

### D1a. Every import step yields, and a hold ends before it is overdue

D1 covered only `apply`. A rehearsal of a large import showed the other steps hold the boundary as long as a request lets them: `plan` ran for as long as its 50 s request budget, a `file` call chained `start`, `select` and `plan` in one hold, and the cell's filer binds no request budget, so each of its steps began with a fresh 50 s. A waiting writer is refused `MUTATION_BUSY` once the hold age reaches the mutation lock's long-holder limit (30 s), well before that.

The same rule now covers every loop of an import step. Each loop checks `writers_waiting()` after it has handled one item, as `apply` does, and stops starting items when the room left to its hold no longer covers the delivery reserve (`_stop_now`):

- `plan`: the read of the earlier imports, per Source page (D1b), and the item loop, per item. It returns its draft cursor, with `stop_reason` for a waiting writer.
- `apply`: per chunk (D1), and the full read of the Sources when the run's seal broke, per Source page.
- `finish`: the removal of staged copies, per copy; the order and the stop rule are below.
- `file`: between steps, and the export expansion, per conversation.
- The cell's filer: between calls (below).

**The contract.** No import step starts an item once the room left to its hold, less the delivery reserve (5 s), is spent. A piece of work that is already running runs to its end. The hold therefore ends before the long-holder limit when every piece that no step can split is shorter than the reserve. The design names the pieces that are longer under "What stays unbounded". It does not promise that no hold is ever overdue.

**One budget, bound as the current one.** `plan`, `apply`, `finish` and `file` run through `_within_hold`, which binds the call's budget cut to the room the hold has left (`request_budget.bound_to`: the hold's long-holder limit minus its age, read from the mutation lock). The cut budget is the current one, so the readers that a step calls stop with it: `find` in `finish`'s recall check, the lexical-store reads under the duplicate check, and the wait for an in-flight path in `reserved_paths`. A call that binds no budget (the REST and CLI doors, the filer) gets a fresh one of the usual length, cut the same way. No second copy of the 30 s limit exists, and the loops stay unaware of it.

**The floors.** A call must progress even when its room is spent, so the read of the Sources reads `INDEX_READ_FLOOR` (2,000) pages before it asks whether to stop. The floor applies to the first step of a call only: a step that follows another has progress already, and asks before each page. A page of the first read costs about 0.4 ms in the measurement below, and about 3.1 ms on real pages by the critic's estimate, so the floor holds the boundary for 0.8 s to 6.2 s past the room.

**`finish` makes progress and is not final until it is complete.** It removes the staged copies first, then runs its tail (recall check, coverage, empty folders, residual, manifest). A removal loop that stops, for a waiting writer or for the budget, keeps the marks it made, saves the run in its phase and returns `stop_reason: interactive_write_waiting` or `call_budget_spent`. The `file` chain re-runs `finish`, which resumes. Each call handles at least one copy, so each makes progress. The tail runs once, in a call that still has the reserve after the removals, so a stop never leaves the remaining copies reported as `unchecked` and never turns a media artifact `unknown` for want of budget.

**The cell's filer.** It calls `file` again at once after each `working` reply, which gives a writer that polls for the boundary no gap, above all a writer in another process. Between calls it now waits while a writer waits (`yield_to_waiting_writes`, within the foreground-priority cap, as the media worker does). Its own wait for the boundary does not count as an interactive writer's: its calls run as background work (`writer_lease.as_background_work`, holder kind `background`), so an agent's own `file` call or the owner's write does not end early to let the filer in. The queued-write replay shares `background_call` and keeps its counted wait.

Rejected: a per-loop deadline constant (a second copy of the limit in each loop); leaving a `finish` that meets a waiter or a short budget as a final call (it would strand staged copies as `unchecked` for a person to review); a narrowed copy of the budget that only the step's own loops read (nested readers would run to the request's full budget).

**What stays unbounded.** These pieces run to their end whatever room is left. The costs were measured with probes of the same code on a generated vault, on a loaded laptop (other sessions were running), unless marked as an estimate. The probe commands are in the PR body.

- `start`: its first page summarises the whole stage with `overview` (collection store ownership read), which reads each staged note under the vault's read guards. Measured on first pages: 3.6 s for 5,000 files and 16.7 s for 20,000 files, so about 0.7 to 0.8 ms per staged file. A stage of 70,000 files would hold the boundary for about 58 s in that first call (an estimate by that rate, not measured at that size). Later pages cost 0.46 s at 20,000 files, because the run keeps the overview. This is the one piece found that passes the limit alone, from about 35,000 staged files. `overview` is shared with the collection overview, has no stop and is not changed here; bounding it needs a resumable overview, which is its own change. After `start` the `file` chain finds its budget spent and starts no further step.
- `select`: it classifies one page of at most 5,000 rows, 0.09 s measured for 1,000 rows.
- `finish`'s tail (recall check, coverage, empty folders, residual, owed rows, manifest) runs in one call after the removals, and starts only when the reserve is left. Measured at 300 filed items: 5.4 s in all, of which 4.2 s is the recall sample (ten `find` calls, about 0.4 s each in a process that has not searched yet), 0.4 s the recall check and 0.7 s the manifest write. At 1,000 items the tail took 16.3 s (not broken down). The sample is a fixed ten finds, so most of the growth is the first search of a larger vault. A page of several thousand items can therefore pass the limit in the tail. `find` reads the bound budget (it skips its rerank and its read checkpoints stop it), so a budget that runs out shortens the sample; how much it shortens the tail is not measured here.
- `Destinations.frozen_by` and `seen_in_plan` of `plan` walk the run's draft once per call: linear in the page's items, at most 5,000.
- `resolve_links` sorts the whole index once, at the plan's last call.
- The run object is saved whole once per call, so a call that ends often saves it often. It grows with the page, not the vault.

### D1b. The Sources index that `plan` reads is remembered per process

A plan read every Source page of the vault at each call, to find the earlier imports of its originals: the cost of a call grew with the Sources already filed, so page 40 of a large import planned slower than page 1 for the same files. `adoption_plan._SOURCE_PAGES` now remembers what each page recorded.

- **What is cached.** For each vault, keyed by its real path, the entries of each Source page (`EarlierImport` values for its original and each `also_imported_from` path) with the file signature the page had when it was read. `ImportIndex` then keys those entries by label and by original path, so a lookup for one original does not test every other Source.
- **Invalidation.** A page is read again when its signature (mtime, ctime, size, inode) differs from the remembered one: the same rule as the note index (`NoteIndex`). A page that changed less than two seconds before the read (`_RACY_NS`) is remembered without a signature, so the next pass reads it again; a timestamp cannot prove a later change inside that window. A pass that reaches every page replaces the vault's map, so a deleted page leaves it. A pass that a stop ended keeps what the vault's map held for the pages it did not reach.
- **Memory.** About 1.6 KB for a Source with one entry, more as `also_imported_from` grows, held for the life of the process. At 70,000 Sources that is about 110 MB (estimate from 1.6 KB per entry; not measured at that size). The map holds at most 4 vaults (`_KNOWN_VAULTS`) and drops the least recently used one.
- **Cost.** A cold read of a vault costs the page reads: 0.4 ms per small generated page in the measurement (2.0 to 2.5 s for 5,000 Sources), and about 3.1 ms per real page by the critic's estimate. A warm read costs one `lstat` per page: 59 to 63 ms for 5,000 Sources. A plan call after a process start pays the cold read once. With `INDEX_READ_FLOOR` and the stop of D1a, a vault larger than the floor is read over several calls.
- **Correctness.** The cache decides nothing that the page does not: a lookup runs the same predicate over the entries, and the disk is never written from it. `apply` reads the same cached index when its Sources seal broke, so a stale entry could let it file a duplicate. The signature rule, with the racy window, is what rules that out: a changed page is read again, and a page too new to trust is read at every pass.

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

- An import under a steady stream of writes returns one chunk, one planned item or one removed copy per call. Each call makes progress, so the import finishes.
- The handoff wait delays a maintenance job by at most the cap per release, and only while a writer waits.
- A writer in another process that never gets served keeps the signal for its wait bound; the handoff wait ends at the cap.

## Known residuals

- Within 24 hours of the upgrade, a retry under an explicit key whose text is not in Unicode NFC gets `IDEMPOTENCY_KEY_REUSED`. The receipt stored before the upgrade holds a digest of the text as sent, and the new digest normalises it first. A retry under a new key commits.
- An import apply interrupted before the upgrade, with paths in NFD, gets `IMPORT_RECOVERY_UNRESOLVED` on resume, for the same reason: its stored request digest predates the normalisation.
