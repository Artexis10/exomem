## Context

Every public write passes `writer_lease.invoke_command`, the one dispatcher that MCP, REST, hosted and CLI share. A keyed write that meets a busy boundary already waits in a fair in-process line (`_FairCaptureQueue`) for a bounded time (`_capture_wait_seconds`, 40 s by default, capped by the request budget), then raises `MUTATION_BUSY` with `committed: false`. Every MCP write is keyed: an explicit idempotency key, or an implicit key from the MCP retry scope. The `IdempotencyStore` gives a same-identity retry its stored terminal, also after a crash.

What is missing is custody. Once the wait runs out, nothing in the service remembers the save.

Primary source for the shape: "Writing effective tools for agents", the row "The model keeps misusing a tool/MCP surface" of the agentic-engineering routing table. A tool whose correct use depends on the model remembering to call it again is the tool's defect. The response must state the outcome the agent can repeat and must not invite a wrong retry.

## Goals and non-goals

Goals: a save that reached the serving process is never lost because the boundary was busy; the caller sees `queued`, never `saved`, until the save lands; a queued save lands once, in arrival order, across a restart; a failed queued save stays visible with its arguments.

Non-goals: shorter boundary holds (owned by other changes); a client-side spool; queueing for a process that runs no drainer (stdio, CLI); adopting `MUTATION_ACKNOWLEDGEMENT_PENDING`, whose write may have committed and which a same-identity retry already resolves.

## Decisions

### The intake lives in the serving process, not in client hooks

Every client the owner uses (Claude Code, Codex, Devin, claude.ai) reaches the same HTTP service, so one intake covers all of them. A hook spool covers only clients with hooks, cannot see the server's idempotency identity, and resends through the same busy boundary. The service also knows whether nothing committed; a client does not.

Only the HTTP serving runtime (`serves_upload_sessions`) starts the drainer, and acceptance requires a running drainer. A stdio server or the CLI keeps today's refusal. The store is per vault state directory, so a later serving process drains what an earlier one accepted.

### Reuse: SQLite in the state directory, the dispatcher and the idempotency store

Maintained queue libraries (persist-queue, huey's SQLite backend) give a durable FIFO, which is the smallest part of this change. The hard part is replaying a save as its principal through the ordinary dispatcher with exactly-once identity, which no library can do. The repository already keeps more than fifty SQLite state stores with the private-state ceremony (`maintenance_jobs` is the closest: it already runs `invoke_command` from a background thread as the starting principal). The intake follows that store pattern and adds no dependency. The store uses WAL with `synchronous=FULL`, so a `queued` receipt survives a power loss as well as a process kill.

### What the intake accepts

A save is accepted only when all of these hold:

1. The intake is on (`EXOMEM_BUSY_INTAKE`, default on) and this process runs the drainer.
2. The command declares `busy_intake` in its registry metadata. The knowledge saves declare it (`remember`, `edit_memory`, `replace_memory`, `observe_memory`, `connect_memory`, `episode_memory`, `capture_source`, `preserve_evidence`, `record_memory`, `plan_memory`). Maintenance, uploads, transfer, credential issuance, capture-attempt protocol steps, erasure and schema or governance changes do not: each either runs as its own job, carries bytes outside its arguments, or must not run later than its caller expects.
3. The refusal proves nothing committed and is caused by contention: `MUTATION_BUSY`, `MUTATION_WARMING` or `COLLECTION_STORE_BUSY` with `committed: false`. These are closed protocol codes. Every other refusal reaches the caller unchanged.
4. The principal is the owner (`raw_protection.is_owner`) with no delegated authorization session. A delegated authority can be narrowed or revoked; replaying it later could exceed what the owner still grants. Such a caller keeps today's refusal. The owner's client binding and origin-session binding are stored and restored, so the replay runs as the same login and any live-session check still applies.
5. The serialised arguments fit in 4 MiB. A larger save keeps today's refusal, which says it was too large to queue.

### The caller gets a queued receipt quickly

When a save is eligible, `_capture_wait_seconds` caps the busy wait at `EXOMEM_BUSY_INTAKE_AFTER_SECONDS` (15 s by default). Today's median refused call took 10 s and some took minutes; an agent should not sit in a blocked call when the service can take custody.

The receipt is a normal result, like a maintenance-job handle: `success: true`, `state: "queued"`, `committed: false`, and `queued_write` with its `ref` (`exomem://queued-write/<id>`) and its position. `user_summary` leads with "Queued, not saved yet" and says not to resend. It is never a `saved` outcome and carries no page title or id, because no page exists yet.

### Order: one drainer, first in first out, and new saves wait behind the queue

One drainer thread applies queued saves by acceptance sequence. A new eligible save that arrives while saves are queued waits for the queue to empty, within the same short wait; if the queue does not empty, the save is queued behind them. Without this, an edit sent after a queued create could run first and fail. Ineligible writes (maintenance) are not ordered against the queue.

### Exactly once

- The drainer replays with its own explicit identity, `intake:<id>`, scoped to the owner's audience. It has the 24-hour explicit retention. A crash after the commit and before the intake row is marked replays the stored terminal on restart instead of writing again.
- The intake row stores the original effective idempotency key. While the row is queued or applying, a resend with that key returns the same receipt and runs nothing. A second identical save sent with the same key therefore cannot overtake or duplicate the queued one.
- The idempotency store claims a replay identity before the attempt waits for the boundary, and it keeps an identity whose owner died mid-attempt as `MUTATION_OUTCOME_UNKNOWN` for good, because it cannot prove the dead attempt did not commit. Retrying that identity would block the queue forever. So the intake row records, durably and before the leaf runs, that an attempt under the current identity held the boundary (through `mutation_lock.on_boundary_acquired`, which now chains observers). The marker is sticky for the identity. After a crash:
  - No attempt under the identity ever held the boundary: no leaf ran, so the drainer replays the save under a fresh identity (`intake:<id>:<n>`). This is the common case, a worker killed while its replay waited in line.
  - An attempt held the boundary: the save may have committed. The drainer replays under the same identity; the store's commit evidence resumes it exactly once, and without evidence the store answers `MUTATION_OUTCOME_UNKNOWN`. That answer is final: the row becomes `failed`, keeps its arguments, and its read says the save may have landed and to check the page before resending.
- A resend with a new identity (a new session) is a new save. The receipt tells the agent not to resend, and the outcome read lets a later session check instead.

### Outcomes

- Applied: the row keeps the compact result (`user_summary`, page title and id where the result names them) and drops the arguments. Applied rows are pruned after 7 days.
- Contention again, or a pending or committed-uncertain answer that a same-identity retry resolves: the row stays queued; the drainer backs off from 1 s up to 30 s and retries without limit. Contention is not a failure.
- Any other refusal: the row becomes `failed` with the public refusal and keeps its arguments. It stays until the owner dismisses it with `exomem intake dismiss <id>`. Dismissal is an owner operation on machine-local state, like `exomem lease release`, so it is a CLI command and not a write mode inside a read tool. The service never edits a queued save to make it fit, for example by refreshing a stale hash guard.
- An unexpected exception in the drainer marks the row failed with code `INTERNAL`, so one bad row cannot stop the queue.

### Visibility

`review_memory(mode="queued-write")` lists queued, applying and failed saves (bounded), oldest first, with command, state, age, attempts and refusal. With `ref` it returns one save, including its arguments while it is queued or failed. `coordination_status` reports `write_intake` counts and the oldest age. An unreadable store reports `unknown`, never zero.

## Risks

- A queued save is not readable until it lands. The receipt says so, and the drainer starts as soon as the boundary frees.
- Arguments of queued and failed saves sit in the state directory. That directory already holds the index and journal copies of the same content, under the same private-state ceremony.
- A long queue under a stuck holder grows without bound in count. Each row is at most 4 MiB, and `coordination_status` shows the backlog.
