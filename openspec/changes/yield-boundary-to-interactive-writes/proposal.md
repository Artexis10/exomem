## Why

An interactive write that arrives while background work holds the vault write boundary can still end in `MUTATION_BUSY`. Two causes remain after `absorb-transient-write-busy`.

- An import apply holds the boundary for a whole call, about a minute of chunks, and does not look at who waits. A `create_note` from the agent's own session or from the CLI waits out its bound (about 30 s) and is refused, although the import could stop at any chunk boundary.
- Background holders yield to a waiter, but not for long enough. The waiting writer polls for the boundary, so the holder that asks again at once takes it back. Against a writer in another process, which no in-process mark makes the holder yield to, this repeats until the writer's bound ends.

A third cause showed on the personal service: a maintenance job held the boundary for about 33 minutes. The reconcile phases for unit sidecars and embeddings ran as one hold over every drifted page, with no waiter check, and their holder label hid the cause (`unknown`).

A retry after a lost reply replays the original commit, but the reply is the same as the first one. The agent cannot tell that nothing was written by the retry.

## What Changes

- An import apply ends its call at a chunk boundary when an interactive writer waits and the call has committed at least one chunk. It returns the existing continuation and a visible `stop_reason`. It never releases the boundary mid-call and adds no run lease.
- A waiter counts for yielding only when it is interactive. A machine hold, a maintenance job and a borrower of a lent boundary do not count. The keyed writes queued behind the head of the capture line do count. A waiter in another process counts.
- After a hold that ended for a waiter, a maintenance job, the reconcile phases and the drain turns wait while a writer still waits, within the existing foreground-priority floors, before they take the boundary again. The wait holds no lock and never runs on the collection store thread.
- The reconcile phases for unit sidecars and embeddings run in short holds that each end after one second or when an interactive writer waits. Every hold names its phase.
- A reply to a replayed write carries `replayed: true`, and its `mutated` and receipt stay those of the original commit. The implicit-retry digest treats one text in two Unicode compositions as one request. The retry-safe-mutations spec states the 600 s window that the code uses.

No wait is lengthened, no governed write is queued, the boundary is not lent to canonical writers, and no outbox is added.

## Capabilities

### Modified Capabilities

- `hosted-mutation-safety`: background holders step aside for interactive waiters only and hand the boundary over; maintenance phases hold in short holds.
- `adoption-studio`: an import apply ends its call for a waiting interactive write.
- `retry-safe-mutations`: a replay says so; the implicit window is stated as 600 s; the digest is Unicode-normalised.

## Impact

- Code: `adoption_run.py`, `mutation_lock.py`, `foreground_priority.py`, `maintenance_jobs.py`, `reconcile.py`, `derived_drain.py`, `writer_lease.py`, `mutation_terminal.py`.
- Behaviour: an import call may return after one chunk when writers wait, so a large import takes more calls. A replayed reply gains `replayed`.
- Windows: the cross-process waiter signal stays process-local, as before.
- Undo: revert the change. No stored file format changes. The request digest of a write whose text is not in Unicode NFC changes, so the design lists two residuals for the first 24 hours after the upgrade.
