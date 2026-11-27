## Why

The busy-write intake returned a `queued` receipt for saves that their own arguments condemned. A recap with an item over its size cap, or with a malformed episode key, came back `queued` and failed only at the replay. The caller had already moved on, so it could not correct the input in the same turn. On 2026-11-25 the personal service recorded seven such failures. Most arrived behind other queued saves, a path that ran no command code before accepting.

## What Changes

- Before the intake accepts a save, the dispatcher applies the refusals that the save's arguments alone decide. A save that one of them refuses gets that refusal at once and is not queued.
- The checks cover compact observation metadata, the size, shape and key of an episode recap, and the argument rules of `record_memory` and `plan_memory`.
- A refusal that needs vault state stays at the replay, because a save behind the queue may depend on a save queued ahead of it.
- A save behind the queue that its arguments condemn is refused without waiting for the queue.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `busy-write-intake`: adds the requirement that a save its arguments condemn is refused, not queued.

## Impact

`commands.refusal_before_queueing`, `episode_memory.argument_refusal` and `write_intake.Admission`. No change to the queued receipt, the store or the replay.
