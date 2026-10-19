## Context

`foreground_priority` parks every `bulk()` pass at its next unit while a
`foreground()` request runs, under a progress floor: one grace wait, then at most
half of the pass's elapsed time. A pass never parks while its thread holds a
mutation boundary. Until this change only activation was marked.

## Decisions

### Mark at the dispatcher and at the boundary, nowhere else

The dispatcher marks every command in one innermost decorator. It marks the thread
that runs the command, which is the collection store's thread for a routed call. It
leaves out the fixed public completion that pads a projected read to its class
deadline, because that padding is a sleep and needs no priority.

The coordinator marks every `hold()`, the wait included. This also covers holders
that no command runs: the graph drain, a rebuild's publication hold and the
watcher's guard sites. A writer queued on the boundary waits out whatever slows the
holder, so the holder is foreground work whoever it is.

The marks nest. The in-flight count now counts threads, not marks: a thread counts
once, however deep its marks go. The per-thread depth stays counted up and down, as
it was, because reserved-state coordination releases its boundaries out of order.

### A wait on bulk-owned work releases the pass that owns it

A foreground thread that joins work a parked pass owns would wait out the pass's
progress floor instead of the work. The progress floor bounds a pass's total wait at
one grace wait plus half of its elapsed time, so in a pass that has run for minutes
one such wait can last many times the work it waits for.

Two scopes prevent it.

- `awaiting_bulk()` releases every pass for the wait. It is for a waiter that may
  hold a mutation boundary: another foreground thread can keep the owner parked, and
  a writer queued on the waiter's boundary would then wait on the waiter, which waits
  on the parked owner. Releasing every pass breaks that chain. During the wait no
  request has priority, activation included, so it is kept for short waits.
- `outside_foreground()` only removes the waiting thread from the in-flight count. It
  is for a waiter that holds no boundary. The pass then runs unless another request
  holds it, and that request keeps its priority.

A thread outside `foreground()` claims nothing, so a background thread that joins a
flight releases no pass.

The waits that take `awaiting_bulk()`:

- the resolver single-flight follower, which may hold the boundary;
- the corpus-context flight join, which may hold the boundary;
- a reader that waits on another reader's graph snapshot proof. Startup graph
  validation runs that proof inside a bulk pass, and the proof's walks yield;
- the recall replacement lock. A whole-vault rebuild holds it across a walk that
  yields, and an inline rebuild on a command thread can wait on it.

The graph rebuild join takes `outside_foreground()`. A thread that holds the boundary
cannot join a rebuild, because the rebuild publishes under the boundary. `reconcile`
joins without a bound, so releasing every pass there would cost every other request
its priority for the whole rebuild.

Other waits need neither:

- The identity-catalogue flight and the catalogue proof flight have no bulk owner.
- A vault creation lock, the lexical publication lock among them, counts as a
  boundary, so its holder never parks.

No other yield in a bulk pass holds a lock that a foreground thread waits on.

### The drain classifies refusals before it isolates receipts

A readiness refusal is a refusal by its own contract (`status: retryable`), a vault
lock timeout, a busy graph epoch, or a SQLite busy or locked result code. On such a
refusal the batch leaves every receipt where it was. The writer's debt signal or the
drain's backoff retries the batch, so the drain never retries in a tight loop.
Isolation runs only for other failures. It replays at least one receipt per pass,
then returns to the loop as soon as a writer waits on the boundary. Stepping aside
before the first replay would let one page that always fails pin every healthy page
queued with it: the failing page rotates behind the queue only when it is replayed.

### A row that drops spans says so

A governed write now records more distinct spans than a ledger row keeps. The row
keeps its slowest 32 spans and adds `spans_truncated`, the number it dropped. The
bound stays at 32: rotation keeps 2,000 rows within 8 MB, about 4 KB a row, and 32
spans that carry fields already fill that. The span producer bounds a call at 64
distinct names, and now sums the spans under names past that bound as one
`spans.overflow` span instead of dropping them.

## Risks

- During a wait inside `awaiting_bulk()` no request has priority over bulk passes,
  activation included, although activation had priority before this change. These
  waits are bounded: a resolver follower's wait (3 seconds on a managed request), a
  2-second join, one proof, one walk.
- A pass runs longer under continuous foreground traffic. The progress floor still
  lets it progress.
- A long CPU-bound command, such as an import, media processing or maintenance,
  holds the bulk passes back for its whole run.
