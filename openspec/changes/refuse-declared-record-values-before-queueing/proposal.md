## Why

The busy-write intake returned a `queued` receipt for a Records save whose value lies outside a field's declared enum. The replay refused the value later, after the caller had moved on. A busy `plan_memory` add, update or triage had the same defect for a status outside the vault's Planning values. The argument-only checks that run before queueing cannot see either refusal, because each reads a declaration from the vault.

## What Changes

- Before a `record_memory` append or update is queued, the service checks its supplied values against the per-field rules of the declared fields they name. It gives a value refusal at once when the writer would give it first. A disabled route, stale guard, held candidate or unresolved short row id leaves the save to replay. An edit also queues if another save is open or enters the intake during its check, because that save can change a guard. Required fields, undeclared names and keys stay at replay.
- Before queueing a Planning save to a store-owned collection, the serving process checks its supplied values against the Planning and field rules. It includes an add’s explicit null. An update’s or triage’s null deletes the field and stays at replay. Rules that depend on several fields, including what an area can carry, stay at replay.
- The busy check shares the store dispatch checks and binds the same writer arguments as the commit. It gives a value refusal only if the store route is open and the commit refuses that value first. An update or triage also needs a readable item named by its plan id and both current guards. If a route is disabled or another check would refuse first, the save queues for replay.
- A Records refusal carries the code, the field, the allowed values and the per-field issues. A Planning refusal carries the code and message that the commit's rule gives for that value. Each holds nothing, names resending as the route, and leaves through the same terminal egress filter as a direct refusal.
- A queued note edit or a Records or Planning save that carries manifest text can change a declaration. The intake marks the queue before reading the declaration. It checks for a possible change at or after the mark before sending a refusal, even if the other save has committed.
- An insert records the latest sequence that may change a declaration. An older writer's unmarked inserts count too. At upgrade, one seek to the last old save covers all legacy saves. The intake never scans the old queue to add the marker.
- The check runs only for a save that is about to queue. It has 0.5 s to decide, including opening the queue, reading coordinator authority and taking a store checkout. The Planning check also has a fixed SQL work budget. If either budget runs out or the refusal cannot be filtered, the save queues. An unfinished coordinator read keeps the single optional read slot until it stops. The service releases any borrowed checkout before it returns the queued receipt, so cleanup can delay that receipt.
- The dispatcher refuses writes in a read-only Cloud cell before intake or store routing.
- An argument refusal that existed before this change is still raised when the egress filter faults. Only the declared refusal is dropped then.
- Decided trade-off: a declaration change made outside the queue and not committed at the check, such as a direct revise that holds the boundary or a save of the Planning values, is not seen. A value that it adds is refused, and the refusal tells the caller to send the same value again once that change has committed.
- A busy Planning save to a file-backed collection queues for replay. An add’s checks can read every item in a file-backed collection. The store reads by key, but an update or triage that uses an `item_id` still needs the store thread to resolve it.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `busy-write-intake`: an argument refusal is raised even when the egress filter faults, and added requirements define the declared Records and Planning refusal before queueing: what each checks, what it leaves queued or to the replay, and its shape.

## Impact

The intake store gains the nullable `declaration_change` column, one state row, and an insert trigger. The trigger keeps the last possible declaration change, including inserts from older releases after rollback. The busy check reads the queue by index and compares its mark with that state row. Planning reads a guarded item by key when needed. The installed cost is unmeasured.
