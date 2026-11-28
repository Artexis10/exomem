## Why

The busy-write intake returned a `queued` receipt for a Records save whose value lies outside a field's declared enum. The replay refused the value later, after the caller had moved on. A busy `plan_memory` add, update or triage had the same defect for a status outside the vault's Planning values. The argument-only checks that run before queueing cannot see either refusal, because each reads a declaration from the vault.

## What Changes

- Before a `record_memory` append or update is queued, the serving process checks the values it supplies against the per-field rules of the declared fields they name: type, format, enum and nested values. A value that those rules refuse gets that refusal at once, and the save is not queued. Required fields, undeclared names and keys stay at the replay.
- Before a `plan_memory` add, update or triage is queued, the serving process checks each supplied value of a field that the Planning values govern with the Planning commit's own rule, then the collection's per-field rules as for Records. An add's explicit null is checked too. An update's or a triage's null deletes its field and stays at the replay. Planning's own single-field rules (the title's length, the fixed lifecycle set, window dates, the bound on results, motivation references) may stay at the replay. Rules that join fields stay there, such as the lifecycle rules and what an area may carry: an item that is or was an area leaves the fields that an area may not carry to the replay.
- An update or a triage whose value that check refuses is refused only where the store that owns the collection reads the item that the save names by `plan_id` and both guards: the guards equal the collection's container hash and the item's version, the caller reads the item whole, and the item does not store the value in that field, which is the commit's own rule. Otherwise the save stays queued.
- A Records refusal carries the code, the field, the allowed values and the per-field issues. A Planning refusal carries the code and message that the commit's rule gives for that value. Each holds nothing, names resending as the route, and leaves through the same terminal egress filter as a direct refusal.
- Any queued note edit (`edit_memory`, the one queueable command that can set any frontmatter field) and any queued Records or Planning save that carries a manifest's text keep a busy Records or Planning save queued instead of refused. The intake marks the queue before it reads the declaration, at a cost that does not grow with the queue. Only a refusal looks at the queue again, from that mark, so such a save keeps the save queued when it was open at the mark or entered the queue after it, also when it has committed in between.
- The check runs only for a save that is about to queue. The save stays queued when the declaration read or a queue read fails or answers after their shared 0.5 s budget, which also bounds opening the queue and waiting for its locks, when the caller cannot read the vault's Planning values, or when the egress filter faults on the refusal.
- An argument refusal that existed before this change is still raised when the egress filter faults. Only the declared refusal is dropped then.
- Decided trade-off: a declaration change made outside the queue and not committed at the check, such as a direct revise that holds the boundary or a save of the Planning values, is not seen. A value that it adds is refused, and the refusal tells the caller to send the same value again once that change has committed.
- Decided trade-off: a busy Planning update or triage of a file collection, or one that names its item by `item_id`, is queued whatever its values. A file collection has no read of one item by its key, and only the store thread's head map binds an `item_id` without a collection scan, while the commit finds the item, and refuses a stale or ambiguous `item_id`, before it judges a value. Its replay refuses an undeclared value.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `busy-write-intake`: an argument refusal is raised even when the egress filter faults, and added requirements define the declared Records and Planning refusal before queueing: what each checks, what it leaves queued or to the replay, and its shape.

## Impact

`commands.declared_refusal_before_queueing`, `record_memory.value_refusal`, `plan_memory.value_refusal`, `planning.supplied_value_refusal`, `collection_refs.decide_on_declaration`, `collection_store.row_binding.edited_row_values` and `whole_row_at` (which the receipt's row label now shares), `operation_entries.manifest_text_commands`, `write_intake.DeclarationWatch`, `write_intake.Admission.certain_refusal` and `Command.sets_any_frontmatter_field`. No change to the queued receipt, the intake store or the replay. The mark reads the queue's last `seq` on its primary key and the first open `seq` on `intake_state`, whatever the queue holds. Only a refusal reads the queue again, from the first `seq` open at the mark, on the primary key. On a local synthetic intake of 100,000 saves with 10,000 open, the mark took 58 SQLite steps, and the read after a refusal took about 130,000 steps and 8 ms. The installed cost is unmeasured. A store-owned Planning update or triage reads its named item only once one of its values is refused without it: one read on the collection's primary key and one on the items' unique `(collection, item key)` index, whatever the collection's size.
