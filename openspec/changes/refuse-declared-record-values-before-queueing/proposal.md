## Why

The busy-write intake returned a `queued` receipt for a Records save whose value lies outside a field's declared enum. The replay refused the value later, after the caller had moved on. A busy `plan_memory` add or update had the same defect for a status outside the vault's Planning values. The argument-only checks that run before queueing cannot see either refusal, because each reads a declaration from the vault.

## What Changes

- Before a `record_memory` append or update is queued, the serving process checks the values it supplies against the per-field rules of the declared fields they name: type, format, enum and nested values. A value that those rules refuse gets that refusal at once, and the save is not queued. Required fields, undeclared names and keys stay at the replay.
- Before a `plan_memory` add, update or triage is queued, the serving process checks each supplied value of a field that the Planning values govern with the Planning commit's own rule, then the collection's per-field rules as for Records. A deprecated or removed value that a row of the collection still stores stays queued, because the commit accepts a value that its item already stores. Rules that join fields, such as the Planning lifecycle rules, stay at the replay.
- A Records refusal carries the code, the field, the allowed values and the per-field issues. A Planning refusal carries the code and message that the same call gets directly. Each holds nothing, names resending as the route, and leaves through the same terminal egress filter as a direct refusal.
- Any queued note edit (`edit_memory`, the one queueable command that can set any frontmatter field) and any queued Records or Planning save that carries a manifest's text keep a busy Records or Planning save queued instead of refused.
- The check runs only for a save that is about to queue. The save stays queued when the declaration read or the queue read fails or misses their shared 0.5 s budget, when the caller cannot read the vault's Planning values, or when the egress filter faults on the refusal.
- An argument refusal that existed before this change is still raised when the egress filter faults. Only the declared refusal is dropped then.
- Decided trade-off: a declaration change made outside the queue and not committed at the check, such as a direct revise that holds the boundary or a save of the Planning values, is not seen. A value that it adds is refused, and the refusal tells the caller to send the same value again once that change has committed.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `busy-write-intake`: an argument refusal is raised even when the egress filter faults, and three added requirements define the declared Records and Planning refusal before queueing: what it checks, when it leaves the save queued, and its shape.

## Impact

`commands.declared_refusal_before_queueing`, `record_memory.value_refusal`, `plan_memory.value_refusal`, `planning.supplied_value_refusal`, `collection_refs.decide_on_declaration`, `operation_entries.manifest_text_commands`, `write_intake.may_change_declaration`, `write_intake.Admission.certain_refusal` and `Command.sets_any_frontmatter_field`. No change to the queued receipt, the intake store or the replay. The cost that the check adds to a busy save is unmeasured beyond the queue-query bench, which timed the probe of open queued saves.
