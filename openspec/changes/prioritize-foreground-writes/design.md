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

The marks nest. A thread counts once in the in-flight count, however deep its marks
go, and the depth is counted up and down because reserved-state coordination releases
its boundaries out of order.

### A wait on bulk-owned work releases every pass

A foreground thread that joins work a parked pass owns would wait out the pass's
progress floor instead of the work. `awaiting_bulk()` around such a wait releases
every pass, not only the owner. Another foreground thread can keep the owner parked:
a writer queued on the boundary that the waiting thread holds would wait on the
waiting thread, which waits on the parked owner. Releasing every pass breaks that
chain. While such a wait runs, other requests get no priority, which is the behaviour
before this change and lasts only as long as the wait. A thread outside `foreground()`
claims nothing, so a background thread that joins a flight does not release the
passes.

The waits that take it are the resolver single-flight follower, the corpus-context
flight join, the graph rebuild join, and a reader that waits on another reader's
graph snapshot proof. Startup graph validation runs that proof inside a bulk pass,
and the proof's walks yield. Other waits need none:

- The identity-catalogue flight and the catalogue proof flight have no bulk owner.
- A vault creation lock, the lexical publication lock among them, counts as a
  boundary, so its holder never parks.
- A rebuild holds the recall replacement lock while it walks, but only background
  reconciles wait on that lock.

No other yield in a bulk pass holds a lock that a foreground thread waits on.

### The drain classifies refusals before it isolates receipts

A readiness refusal is a refusal by its own contract (`status: retryable`), a vault
lock timeout, a busy graph epoch, or a SQLite busy or locked result code. On such a
refusal the batch leaves every receipt where it was. The writer's debt signal or the
drain's backoff retries the batch, so the drain never retries in a tight loop.
Isolation runs only for other failures, and it returns to the loop as soon as a
writer waits on the boundary.

## Risks

- A long wait inside `awaiting_bulk()`, such as `reconcile` joining a whole rebuild,
  returns other requests to their speed before this change for that time.
- A pass runs longer under continuous write traffic. The progress floor still bounds
  that cost.
