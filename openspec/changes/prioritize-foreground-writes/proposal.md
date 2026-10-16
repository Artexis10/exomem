## Why

A whole-vault graph pass, a lexical rebuild and a resolver warm run as CPU-bound
Python in the serving process. Each request thread releases the GIL around every
short SQLite call, stat and fsync, and then waits up to the switch interval to get it
back. Only activation was marked as a foreground request, so the bulk passes paused
only for activation. Governed writes, the drain and publication holds that block
those writes, and every other read ran several times slower while a pass ran.

The written rule in `foreground_priority` limited the mark to read-only work. It
feared a foreground thread that waits on bulk work the mark has parked. That risk
is real, but it belongs to a few named waits, not to every write.

## What Changes

- Mark every command that the shared dispatcher runs, read or write, as a foreground
  request, from one place in `writer_lease.invoke_command`.
- Mark every wait for and every hold of a mutation boundary as foreground, in
  `VaultMutationCoordinator.hold`, so that a drain or a publication holding the
  boundary also runs at full speed.
- Keep the rule that makes this sound: no foreground thread waits on a parked bulk
  pass. A boundary holder never parks. A foreground thread that joins work a bulk
  pass may own (the resolver single-flight build, the corpus-context flight, another
  reader's graph snapshot proof, the recall replacement lock) waits inside
  `awaiting_bulk()`, which lets every bulk pass run until the wait ends. A thread
  that joins a registered rebuild only leaves the in-flight count for the join.
- Have the graph drain leave its whole batch queued on a readiness refusal (a busy
  boundary, a held SQLite store, a busy graph epoch). Between isolated receipts it
  steps aside when a writer waits on the boundary, after one replay per pass.
- Add content-free spans for a page creation's commit stages, the post-commit
  blocks, the default write advisory and the terminal filter on the mutation path.
- Make a ledger row that drops spans say how many it dropped (`spans_truncated`),
  and sum spans past the producer's name bound as `spans.overflow`.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `proportional-write-projections`: in-process bulk passes run behind governed
  writes, boundary holders and reads, and no foreground thread waits on a parked
  bulk pass.
- `call-ledger`: a row whose spans reached the count bound records how many it
  dropped.

## Non-Goals

- Moving the whole-vault pass into a child process.
- Changing what a bulk pass computes, publishes or proves. This change is
  scheduling only.
- Changing the progress floor of a bulk pass (`MAX_WAIT_SHARE`).
