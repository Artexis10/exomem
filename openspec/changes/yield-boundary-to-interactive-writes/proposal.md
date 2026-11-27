## Why

An interactive write that arrives while background work holds the vault write boundary can still end in `MUTATION_BUSY`. These causes remain after `absorb-transient-write-busy`.

- An import apply holds the boundary for a whole call, about a minute of chunks, and does not look at who waits. A `create_note` from the agent's own session or from the CLI waits out its bound (about 30 s) and is refused, although the import could stop at any chunk boundary.
- An import `plan`, a `finish` and the steps of one `file` call have the same gap, and each holds the boundary for as long as its request lets it. The cell's filer binds no request budget, so each of its calls gets a fresh 50 s. A waiting writer is refused once the hold passes the mutation lock's 30 s limit.
- A large vault makes `plan` slow at each call: it read every Source page of the vault each time, so a call cost the Sources already filed, and one call could outlast the limit before it planned an item.
- A `finish` that ends early for a short budget leaves its remaining staged copies reported as `unchecked` and turns the media artifacts of the page `unknown`.
- Background holders yield to a waiter, but not for long enough. The waiting writer polls for the boundary, so the holder that asks again at once takes it back. The cell's filer is one: it calls `file` again at once after each reply. Against a writer in another process, which no in-process mark makes the holder yield to, this repeats until the writer's bound ends.
- The filer's own wait for the boundary counts as an interactive writer's, so an agent's own `file` call ends early to let the filer in.
- On the personal service, a maintenance job held the boundary for about 33 minutes. The reconcile phases for unit sidecars and embeddings ran as one hold over every drifted page, with no waiter check, and their holder label hid the cause (`unknown`).

A retry after a lost reply replays the original commit, but the reply is the same as the first one. The agent cannot tell that nothing was written by the retry.

## What Changes

- An import apply ends its call at a chunk boundary when an interactive writer waits and the call has committed at least one chunk. It returns the existing continuation and a visible `stop_reason`. It never releases the boundary mid-call and adds no run lease.
- An import `plan`, a `finish` and the steps of a `file` call end at an item or step boundary for a waiting interactive writer too. Each stops starting items when the room left to its hold no longer covers the delivery reserve, whether or not the call binds a request budget. The hold ends before it turns overdue, except for the pieces of work that no step can split, which the design names with their cost.
- The call's budget is cut to the hold's room and bound as the current one, so the readers a step calls (`find`, the lexical store, the wait for an in-flight path) stop with it.
- A `finish` that stops for a waiting writer or a short budget keeps the removals it made and resumes in its next call. The recall check, the coverage and the manifest run once, in a call that removed no copy, so they have the hold's whole room. A call that removed copies ends with `stop_reason: finish_check_next_call`.
- An item loop (`plan`, the removal of staged copies, the reading of an app export) works for a turn of 1 s before a waiting writer ends its call, so two importers that file one stage each move its page on at every call and both finish. A rehearsal of a 69,300-note stage showed the livelock, in which two callers took turns at one item per call.
- The reading of a stage's app exports is a step of its own, which ends the call, so the scan that meets the transcripts has a hold of its own.
- A Source page that the filesystem refuses for now ends a plan's read of the earlier imports as incomplete. It is no longer read as a page with no imports.
- A `finish` starts a search only while its room covers the delivery reserve, ends its recall sample at the first search that a warming index cannot answer, and reports such a search as `pending`, never `failed`.
- The cell's filer backs off a stage whose `working` replies repeat the reply before them 5 times in a row, with a visible reason and no support request, and calls again after a delay of 1 to 15 minutes, instead of calling again at once. A step that moves with no count changing, such as a cold read of the Sources or a run of removals, shows it in the page position and is not a stall. The Cloud CLI's driver uses the same limit.
- The cell's filer waits between its calls while a writer waits, and its calls wait for the boundary as background work, which no writer yields to.
- The cell's filer makes no `file` call on a stage while an agent files it, and carries on after a quiet window (twice the limit of a hold) in which no agent has called. The stage stays `working`, and the filing view says in a `note` that an agent files it. Its own `file` call no longer lists the stage again, which dropped the delays that its calls returned.
- `plan` remembers the earlier imports of each Source page for the life of the process, per vault, and reads again only the pages that changed. A call that has no room reads a first floor of 2,000 pages and moves the read on at each call. The design records the cache's invalidation, memory and cost.
- A waiter counts for yielding only when it is interactive. A machine hold, a maintenance job and a borrower of a lent boundary do not count. The keyed writes queued behind the head of the capture line do count. A waiter in another process counts.
- After a hold that ended for a waiter, a maintenance job, the reconcile phases and the drain turns wait while a writer still waits, within the existing foreground-priority floors, before they take the boundary again. The wait holds no lock and never runs on the collection store thread.
- The reconcile phases for unit sidecars and embeddings run in short holds that each end after one second or when an interactive writer waits. Every hold names its phase.
- A reply to a replayed write carries `replayed: true`, and its `mutated` and receipt stay those of the original commit. The implicit-retry digest treats one text in two Unicode compositions as one request. The retry-safe-mutations spec states the 600 s window that the code uses.

No wait is lengthened, no governed write is queued, the boundary is not lent to canonical writers, and no outbox is added.

## Capabilities

### Modified Capabilities

- `hosted-mutation-safety`: background holders step aside for interactive waiters only and hand the boundary over; maintenance phases hold in short holds.
- `adoption-studio`: an import apply, plan, finish and the steps of a `file` call end for a waiting interactive write, and stop starting work before the hold is overdue; two importers on one stage each move it on at every call.
- `retry-safe-mutations`: a replay says so; the implicit window is stated as 600 s; the digest is Unicode-normalised.

## Impact

- Code: `adoption_run.py`, `adoption_plan.py`, `export_expansion.py`, `request_budget.py`, `import_filing.py`, `mutation_lock.py`, `foreground_priority.py`, `maintenance_jobs.py`, `reconcile.py`, `derived_drain.py`, `writer_lease.py`, `mutation_terminal.py`.
- Behaviour: an import call may return after one chunk, a turn of items or a turn of copies when writers wait, so a large import takes more calls. A `finish` that removed copies takes one call more for its checks, and a stage with app exports takes about two calls and one scan more. A `finish` stops with `stop_reason` and resumes. A replayed reply gains `replayed`.
- Memory: the Source-page cache holds about 1.6 KB per single-entry Source, for the life of the process, for at most 4 vaults.
- Windows: the cross-process waiter signal stays process-local, as before.
- Residual: a `start` or `rescan` over a stage of about 69,300 files holds the boundary for as long as its scan takes: `start` held it for 19.9 s in rehearsal run 45941473643 (journal row of 05:05:18Z, hold 19,940 ms), so a scan passes the 30 s limit at about 100,000 files (an estimate by that rate). Each `plan` call pays a Sources walk that grows with the Sources filed: 0.3 s at 5,000 Sources, 1.4 s at 15,000, about 6 s projected at 69,300. A resumable overview for import stages, the next change of this programme, bounds both. A `plan` call held the boundary for 28.2 s once in that run; a plan, chunk or filer call that refuses a writer is a defect of this change. This change counts the refused writes, by cause, and does not remove a refusal that a scan causes.
- Undo: revert the change. No stored file format changes. The request digest of a write whose text is not in Unicode NFC changes, so the design lists two residuals for the first 24 hours after the upgrade.
