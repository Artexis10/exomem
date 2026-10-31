## Why

A caller can still get `MUTATION_BUSY` because another writer or background job holds the vault mutation boundary for a few seconds. The caller cannot act on that answer except to send the same write again, which is a wait the server can do itself. Keyed writes already wait out ordinary contention, and every MCP write carries an implicit key, so MCP writes already wait. Three paths still refuse:

- A REST write without an `Idempotency-Key` header, and an in-process CLI or terminal UI write without `EXOMEM_IDEMPOTENCY_KEY`, carry no key. They are refused on the first acquire timeout.
- A direct `/upload` commit takes the boundary with no wait beyond one acquire timeout and answers 409. A tus upload session whose commit stayed busy waits as `retryable` until the client reads the session again.
- Background holders (graph republication, derived rows, collection store ticks) step aside only for waiters in their own process. A write from another process on the same host waits out the whole background hold.

## What Changes

- A REST mutation without an `Idempotency-Key` header resolves an implicit retry identity from its authenticated principal and canonical arguments, as an MCP mutation does. It then waits out ordinary contention, and a resend after a lost reply replays or waits on the first attempt.
- An in-process CLI or terminal UI mutation without an explicit key waits out ordinary contention inside the invocation, with no replay receipt. A later identical invocation still runs as a new write, and a gate that needs a reusable identity still refuses it.
- A remote mutation with no explicit key and no stable retry scope keeps today's immediate refusal. A client that gave up during a wait and resent the write would otherwise commit it twice.
- The direct `/upload` commit paths wait out ordinary contention within the request budget, under the same waiter cap and refusal causes as keyed writes. The server re-attempts a tus session commit that met a busy or warming boundary without waiting for the client's next read.
- A process that waits for the boundary holds a shared advisory lock on a sidecar of the boundary's lock file. A background holder in any process on the host sees that waiter and steps aside at its next unit boundary.

Out of scope, owned by other changes: the store thread's busy hand-back (`shorten-mutation-critical-section`), writes that wait out warm-up (`wait-out-write-warmup`), and collection reads that re-prove ownership without the boundary (PR #2224).

## Capabilities

### Modified Capabilities

- `hosted-mutation-safety`: REST and in-process writes gain a retry identity; upload commits wait out ordinary contention; background holders step aside for waiters in any process on the host.

## Impact

- Code: `src/exomem/server_rest.py`, `src/exomem/product_invoke.py`, `src/exomem/mutation_lock.py`, `src/exomem/server_transfer.py`, `src/exomem/upload_sessions.py`.
- Behaviour: an identical unkeyed REST write from the same principal within the implicit replay window (600 s) replays the first result, as on MCP today.
- Undo: `EXOMEM_CAPTURE_CONTENTION_ABSORB=0` restores immediate refusals for every keyed write; each slice reverts on its own.
