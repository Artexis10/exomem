## Why

When the vault mutation boundary stays busy, a save returns `MUTATION_BUSY`, writes nothing, and tells the caller to retry. The save then survives only if the agent remembers to resend it. An agent that moves on, compacts or ends its session loses that knowledge, and the owner cannot track which conversation still owes a save. On 22 November 2026 the personal service refused 51 saves this way: `observe_memory`, `episode_memory`, `record_memory`, `preserve_evidence`, `capture_source`, `edit_memory` and others. The median refused call lasted 10 s and the longest 1,295 s.

Shorter holds make contention rarer. They cannot make it harmless, because some boundary holder will always run long. The service must take responsibility for a save it has received.

## What Changes

- The serving process accepts a save durably when the boundary stays busy. The save goes into a machine-local intake store and the caller gets a receipt with state `queued`. The receipt says that the save is not applied yet.
- The service waits a short time before it queues a save (15 s by default), not the full contention wait. An agent gets a receipt quickly instead of a blocked call that ends in a refusal.
- One drainer in the serving process applies queued saves in the order they arrived. It runs each save through the ordinary dispatcher, as the principal that sent it. A busy boundary only delays a queued save; the drainer retries it until it lands.
- A queued save lands exactly once, also when the service stops mid-queue. The drainer gives each queued save its own durable replay identity. While the save is queued, a resend with the original identity returns the same receipt and runs nothing.
- A new save that arrives while earlier saves are queued waits behind them, so a queued create is applied before a later edit of the same page.
- A queued save that fails, for example because its page changed and its hash guard no longer matches, stays visible. It keeps its arguments and its refusal, so the caller can correct it and send it again. The service never applies it silently in a changed form and never deletes it by itself.
- `review_memory(mode="queued-write")` lists the queued and failed saves, and with `ref` reads one save's outcome. `coordination_status` counts queued, applying and failed saves and gives the age of the oldest. `exomem intake list` and `exomem intake dismiss <id>` let the owner inspect and remove a failed save.
- Each write command declares in the command registry whether it accepts intake. Saves of knowledge accept it. Maintenance, uploads, credential issuance, capture protocol steps and erasure do not.

## Capabilities

### New Capabilities

- `busy-write-intake`: durable acceptance of a busy save, the queued receipt, ordered exactly-once application, visible failure, and the outcome read.

### Modified Capabilities

None. A save that the intake does not accept keeps today's `MUTATION_BUSY` contract unchanged.

## Impact

- Code: a new `write_intake.py` (store and drainer); `writer_lease.py` (an `invoke_command` decorator and the shorter wait); `commands.py` (the `busy_intake` registry field, the `review_memory` mode, the `coordination_status` counts); the `exomem intake` CLI; `server_runtime.py` (start and stop the drainer); `reserved_paths.py` (the new state store); the shipped `mutation-results` reference.
- Behaviour: an eligible save that would have returned `MUTATION_BUSY` returns a `queued` receipt instead. Clients outside the serving HTTP process (a stdio server, the CLI) keep today's refusal.
- Storage: a new `.write-intake.sqlite` in the vault's state directory. It holds a queued save's arguments until the save is applied, and a failed save's arguments until the owner removes them.
- Undo: set `EXOMEM_BUSY_INTAKE=0` and restart, which turns acceptance off and keeps draining what is queued; or revert the change. The new store is not read by anything else.
