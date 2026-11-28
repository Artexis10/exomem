## Why

The busy-write intake returned a `queued` receipt for a Records save whose value lies outside a field's declared enum. The replay refused the value later, after the caller had moved on. A busy `plan_memory` add, update or triage had the same defect for a status outside the vault's Planning values. The argument-only checks that run before queueing cannot see either refusal, because each reads a declaration from the vault.

## What Changes

- Before a `record_memory` append or update is queued, the serving process checks the values it supplies against the per-field rules of the declared fields they name: type, format, enum and nested values. A value that those rules refuse gets that refusal at once, and the save is not queued. Required fields, undeclared names and keys stay at the replay.
- Before queueing a Planning save to a store-owned collection, the serving process checks its supplied values against the Planning and field rules. It includes an add’s explicit null. An update’s or triage’s null deletes the field and stays at replay. Rules that depend on several fields, including what an area can carry, stay at replay.
- The busy check shares the store dispatch checks and binds the same writer arguments as the commit. It gives a value refusal only if the store route is open and the commit refuses that value first. An update or triage also needs a readable item named by its plan id and both current guards. If a route is disabled or another check would refuse first, the save queues for replay.
- A Records refusal carries the code, the field, the allowed values and the per-field issues. A Planning refusal carries the code and message that the commit's rule gives for that value. Each holds nothing, names resending as the route, and leaves through the same terminal egress filter as a direct refusal.
- Any queued note edit (`edit_memory`, the one queueable command that can set any frontmatter field) and any queued Records or Planning save that carries a manifest's text keep a busy Records or Planning save queued instead of refused. The intake records this once on each save when it accepts it, because a replay drops a save's arguments. It marks the queue before it reads the declaration, and only a refusal looks at the queue again, from that mark, for a save whose record is not 0. Each look costs a few index seeks whatever the queue holds. So such a save keeps the save queued when it was open at the mark or entered the queue after it, also when it has committed in between. An ordinary save that commits in between does not.
- A save without a record counts as one that may change a declaration. An earlier release that queues a save after a rollback leaves it without one, and so does an intake store from before this change for every save it holds. Such a store gains the empty column and its index at its next acceptance, which reads no stored save.
- The check runs only for a save that is about to queue. The save stays queued when the declaration read or a queue read fails or answers after their shared 0.5 s budget, when the caller cannot read the vault's Planning values, or when the egress filter faults on the refusal. The budget also bounds opening the queue and waiting for its locks: each statement waits only for the time that the budget has left, and none once it is spent.
- An argument refusal that existed before this change is still raised when the egress filter faults. Only the declared refusal is dropped then.
- Decided trade-off: a declaration change made outside the queue and not committed at the check, such as a direct revise that holds the boundary or a save of the Planning values, is not seen. A value that it adds is refused, and the refusal tells the caller to send the same value again once that change has committed.
- A busy Planning save to a file-backed collection queues for replay. An add’s checks can read every item in a file-backed collection. The store reads by key, but an update or triage that uses an `item_id` still needs the store thread to resolve it.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `busy-write-intake`: an argument refusal is raised even when the egress filter faults, and added requirements define the declared Records and Planning refusal before queueing: what each checks, what it leaves queued or to the replay, and its shape.

## Impact

The intake store gains the nullable `declaration_change` column and the partial covering index `intake_declaration_change`. An older release can still queue saves, and the current release treats its rows as possible declaration changes. The check reads the queue by index and reads a Planning item by key when it needs the item’s current values. A possible Planning refusal runs the store’s dispatch and writer checks within the same 0.5 s budget. The installed cost is unmeasured.
