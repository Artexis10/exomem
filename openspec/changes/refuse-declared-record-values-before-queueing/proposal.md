## Why

The busy-write intake returned a `queued` receipt for a Records save whose value lies outside a field's declared enum. The replay refused the value later, after the caller had moved on. The argument-only checks that run before queueing cannot see this refusal, because it reads the collection's declaration from the vault.

## What Changes

- Before a `record_memory` append or update is queued, the serving process checks the values it supplies against the per-field rules of the declared fields they name: type, format, enum and nested values. A value that those rules refuse gets that refusal at once, and the save is not queued. Required fields, undeclared names and keys stay at the replay.
- The refusal carries the code, the field, the allowed values and the per-field issues. It holds nothing, names resending as the route, and leaves through the same terminal egress filter as a direct refusal.
- Any queued note edit (`edit_memory`, the one queueable command that can set any frontmatter field) and any queued Records save that carries a manifest's text keep a busy Records save queued instead of refused.
- The check runs only for a save that is about to queue. The save stays queued when the declaration read or the queue read fails or misses their shared 0.5 s budget, or when the egress filter faults on the refusal.
- An argument refusal that existed before this change is still raised when the egress filter faults. Only the declared refusal is dropped then.
- Decided trade-off: a declaration change made outside the queue and not committed at the check, such as a direct revise that holds the boundary, is not seen. A value that it adds is refused, and the refusal tells the caller to send the same value again once that change has committed.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `busy-write-intake`: an argument refusal is raised even when the egress filter faults, and three added requirements define the declared Records refusal before queueing: what it checks, when it leaves the save queued, and its shape.

## Impact

`commands.declared_refusal_before_queueing`, `record_memory.value_refusal`, `write_intake.may_change_declaration`, `write_intake.Admission.certain_refusal` and `Command.sets_any_frontmatter_field`. No change to the queued receipt, the intake store or the replay. The cost that the check adds to a busy save is unmeasured beyond the queue-query bench, which timed the probe of open queued saves.
