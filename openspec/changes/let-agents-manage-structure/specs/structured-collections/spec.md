## ADDED Requirements

### Requirement: Collections change shape through one restructure
`change_records` and `change_plans` SHALL accept a `restructure` operation that takes a collection, an ordered list of steps and a concise `why`. The steps SHALL be a closed set: `add_field`, `rename_field`, `retype_field`, `retire_field`, `restore_field`, `rekey`, `merge_into`, `split_out`, `retitle`, `retire` and `restore`. `preview: true` SHALL return the plan and write nothing. `undo` naming a shape-history entry SHALL apply that entry's recorded inverse.

#### Scenario: A rename carries every value and undoes in one call
- **WHEN** an agent renames a field of a collection with 40 rows and later calls `undo` on that entry
- **THEN** every row holds its value under the new name after the rename, and every row holds it under the old name after the undo

### Requirement: A restructure commits only when its round trip is lossless
Before commit, the server SHALL apply the steps and then their inverse, and compare the outcome with the prior manifest and every prior row. Equal SHALL mean the same stored value, so a number stored as 2.0 does not equal the integer 2. The decision SHALL come from the actual values, never from a fixed table of allowed type changes. It SHALL commit only when they are equal, and otherwise refuse naming the step and the rows that fail, changing nothing. No step SHALL delete a stored value.

#### Scenario: A lossy retype is refused
- **WHEN** a restructure retypes a number field to integer and 3 rows hold a fraction
- **THEN** the server refuses naming the 3 rows, and the collection is unchanged

### Requirement: A restructure refuses when a dependent would stop validating
Every restructure SHALL also refuse, naming the dependent, when a dependent of the changed part would no longer validate. Dependents are saved views, references in either direction, rollups, derived rows, filename recipes and the Planning hierarchy.

#### Scenario: A restructure that would break a saved view is refused
- **WHEN** a restructure renames a field that a saved view filters on
- **THEN** the server refuses naming the view, and nothing changes

### Requirement: A merge keeps the union of both collections
`merge_into` SHALL copy every source row into the target and retire the source with the target as its successor; the source's rows SHALL stay readable by its identity. It SHALL fold a source row into a target row with the same natural key only when every value that both rows hold is equal. Where values conflict, the merge SHALL refuse, name the rows and the values, and move nothing.

#### Scenario: A merge with conflicting values moves nothing
- **WHEN** two collections share a key whose rows hold different values for one field, and a restructure merges them
- **THEN** the server refuses naming the rows and the conflicting values, and both collections are unchanged

#### Scenario: A merge with agreeing rows keeps the union
- **WHEN** two collections share 2 keys whose rows hold equal values, and one merges into the other
- **THEN** the target holds every row of both with the 2 shared keys folded once, and the source is retired with the target as its successor
- **AND** a read by the source's identity still returns its rows, marked retired

### Requirement: A merge undo restores both collections
The undo of a merge SHALL retract the copied rows from the target, put back each folded row's prior version and restore the source. It SHALL refuse, naming the row, when a copied or folded row holds other values in the target since.

#### Scenario: A merge undo after a copied row changed is refused
- **WHEN** a row that a merge copied into the target holds other values since, and an agent calls `undo` on that merge
- **THEN** the server refuses naming the row, and both collections keep every row

### Requirement: A split and a rekey keep every row
`split_out` SHALL move the selected rows into a new collection with the same fields and key; its undo SHALL copy them back and retire the new collection with the source as its successor. `rekey` SHALL require every row to hold a non-null and distinct value under the new key.

#### Scenario: A rekey keeps both occurrences
- **WHEN** a collection keyed by part receives a rekey to part and date, and every row holds a date
- **THEN** the restructure commits, every row keeps its values, and a second occurrence of one part can be added as its own row

### Requirement: A row move on a store commits in one transaction
On a store vault, a step that moves rows between keys or collections SHALL commit in one transaction as existing store operations: a revise that carries the shape-history entry, one retraction per row that leaves its key or collection, and one bulk upsert per collection that takes rows. The history chain SHALL verify them like any operation. Where a collection renders a view file per row, the transaction SHALL pass a retracted row's view path to the row that takes it or retire its view file.

#### Scenario: A store merge commits as one unit
- **WHEN** a merge on a store vault fails after its first batch of rows
- **THEN** no copied row, retraction or history entry is committed

### Requirement: A move that a substrate cannot apply refuses
On a collection kept only in files, `rekey`, `merge_into` and `split_out` SHALL refuse `RESTRUCTURE_MOVE_UNAVAILABLE` and change nothing. Where no running service serves a store that an ownership marker enrols, `split_out` SHALL refuse `RESTRUCTURE_MOVE_UNAVAILABLE` and change nothing.

#### Scenario: A move on a collection kept only in files changes nothing
- **WHEN** an agent rekeys, merges or splits a collection kept only in files
- **THEN** the server refuses `RESTRUCTURE_MOVE_UNAVAILABLE` with a plain way forward, and every row and file is unchanged

### Requirement: A split on an enrolled store creates its collection first
On a store that an ownership marker enrols, `split_out` SHALL make its new collection through the store's admitted create, committed before the moves. A failure between the two SHALL lose no row, and a retry of the same split SHALL complete it. An automatic split SHALL pass every automatic bound before that create, so a refused split makes no collection.

#### Scenario: A split interrupted after its create completes on retry
- **WHEN** the service stops after a split's admitted create and before its moves, and the agent repeats the same split
- **THEN** the store holds one new collection with every selected row once, and both history chains verify from genesis

### Requirement: A restructure records its inverse and undoes without loss
The restructure SHALL record the inverse steps, each moved row's origin, and both prior versions of each folded row. An undo SHALL lose no value, including values written since the restructure, and SHALL record its own history entry. An undo that later rows make lossy SHALL refuse naming those rows and change nothing.

#### Scenario: An undo that later rows make lossy is refused
- **WHEN** rows written after a rekey collide under the old key and an agent calls `undo` on that rekey
- **THEN** the server refuses naming the colliding rows, and the collection keeps its current shape and every row

### Requirement: A restructure commits within one bounded call
The leaf SHALL reuse the store's manifest versions and transactions on a store vault, and the collection's audit chain on a file-only vault. One call SHALL write and retract at most 256 rows together, a moved row counting twice, so that its one transaction stays inside the Records hold budget. A larger restructure SHALL refuse `RESTRUCTURE_TOO_MANY_ROWS`, naming the row count and a way forward, and change nothing.

#### Scenario: A restructure larger than one call is refused
- **WHEN** a restructure would rewrite 600 rows of a store collection
- **THEN** the server refuses `RESTRUCTURE_TOO_MANY_ROWS` naming 600 rows and the 256 maximum, and every row and the manifest are unchanged

### Requirement: Retiring a collection takes it off the agent's surfaces
`retire` SHALL keep a collection's manifest, rows and history. A retired collection SHALL leave `search_memory`, `recall_context`, the Records routing advisory, the `collection_candidate` finding and the default inventory. `restore` SHALL return the collection to every surface it left.

#### Scenario: A retired collection leaves recall and stays readable
- **WHEN** an agent retires a collection and then searches for a value that only its rows hold
- **THEN** the search returns none of its rows, and a read by the collection's identity returns them marked retired

#### Scenario: Restore brings a collection back
- **WHEN** an agent restores a retired collection
- **THEN** the collection returns to the default inventory and recall with every row it held

### Requirement: A retired collection answers by identity and refuses new rows
A read of a retired collection by its identity SHALL still answer, marked retired, with its successor when a merge retired it; a merge SHALL record the target's identity as the retired source's `successor`. A new row SHALL be refused with a pointer to the successor, or to `restore` when there is none. A retired collection SHALL keep its title, and `add_record` with `new_collection` naming that title SHALL be refused with a pointer to `restore`.

#### Scenario: A write to a merged-away collection points to its successor
- **WHEN** an agent adds a row to a collection that a merge retired
- **THEN** the write is refused with a pointer to the target collection, and nothing is written

### Requirement: A refusal names a retired collection's successor in plain words
The refusal of a write to a retired collection SHALL name the successor by its title where the title check that releases a page title releases it, and by a plain phrase otherwise. It SHALL name no identifier and no path.

#### Scenario: A refused write names the successor by its title
- **WHEN** an agent adds a row to a collection that a merge retired into a collection titled "Grinders"
- **THEN** the refusal names "Grinders" as where the row belongs, and contains no identifier and no path

### Requirement: A retired field keeps its values
`retire_field` SHALL keep every stored value of the field. A retired field SHALL leave the schema that agents read and SHALL refuse new values; a query SHALL return its values on request. `restore_field` SHALL bring it back.

#### Scenario: A retired field keeps its values
- **WHEN** an agent retires a field that 12 rows hold and later restores it
- **THEN** the field leaves the schema agents read while retired, and all 12 values are present after the restore

### Requirement: The inventory counts retired collections and nothing is deleted
The inventory SHALL always report the number of retired collections and SHALL list them when `inspect` is called with `include_retired`. No restructure step SHALL delete a collection, a row or a value, and this capability SHALL add no permanent deletion path.

#### Scenario: A retired collection is counted and listed on request
- **WHEN** one collection is retired and an agent calls `inspect`, then `inspect` with `include_retired`
- **THEN** the first inventory reports one retired collection without listing it, and the second lists it

### Requirement: An automatic restructure is bounded by the server
`restructure` SHALL accept `automatic: true` for a change the agent starts on its own; without it, a restructure SHALL behave as a requested action. With it, the server SHALL refuse `AUTOMATIC_RESTRUCTURE_REFUSED`, naming the failed bound and changing nothing, unless this requirement and the size and rate bounds below hold:
- the effective `proactive_capture` disposition is `silent`;
- no step changes a decision whose recorded origin is `user_choice` or `user_design`.

#### Scenario: Silent capture applies a bounded restructure
- **WHEN** `proactive_capture` is `silent` and an agent rekeys a collection with `automatic: true`, and every bound holds
- **THEN** the restructure commits and its shape-history entry records origin `automatic`

#### Scenario: Advisory capture refuses an automatic restructure
- **WHEN** `proactive_capture` is `advisory` and an agent restructures with `automatic: true`
- **THEN** the server refuses `AUTOMATIC_RESTRUCTURE_REFUSED` naming the disposition, and nothing changes

#### Scenario: An automatic step never changes the user's design
- **WHEN** an agent renames, with `automatic: true`, a field that a `user_design` entry created
- **THEN** the server refuses `AUTOMATIC_RESTRUCTURE_REFUSED` naming the decision, and the same rename without the flag after the user confirms commits

### Requirement: An automatic restructure stays within the size and rate bounds
With `automatic: true`, the server SHALL also refuse `AUTOMATIC_RESTRUCTURE_REFUSED` unless:
- a change that adds fields, including a merge, to a collection created automatically leaves at most 12 item fields, the same constant and rule as the automatic-creation cap;
- the collection has had fewer than 3 automatic restructures in the last 24 hours, where an undo and a field addition do not count;
- a `split_out` that creates a collection also fits the daily automatic-creation bound.

#### Scenario: The daily bound stops churn
- **WHEN** a collection had 3 automatic restructures in the last 24 hours and an agent starts a fourth that is not an undo or a field addition
- **THEN** the server refuses `AUTOMATIC_RESTRUCTURE_REFUSED` naming the daily bound

#### Scenario: An undo is not counted
- **WHEN** a collection had 3 automatic restructures in the last 24 hours and an agent undoes the last one with `automatic: true`
- **THEN** the undo commits

### Requirement: An automatic refusal names the way forward and the bounds hold everywhere
The refusal SHALL say to propose the change in plain words and apply it after the user confirms. The bounds SHALL be defined once, beside the check, and SHALL hold on a store vault and a file-only vault alike. The daily count SHALL read the collection's own shape history and never scan all history.

#### Scenario: The refusal proposes in plain words
- **WHEN** the server refuses an automatic restructure for a bound
- **THEN** the refusal names the bound and says to propose the change in plain words and apply it after the user confirms

### Requirement: `new_fields` runs the same field check as `add_field`
`new_fields` on `add_record` and `edit_record` SHALL run the same field check and write the same shape-history entry as `add_field`.

#### Scenario: An added detail is checked and recorded
- **WHEN** an agent adds a row with `new_fields` that keep the collection within 12 fields
- **THEN** the fields are added, and the collection's shape history holds one `add_field` entry with origin `automatic`

### Requirement: Every change writes one shape-history entry
Every create, restructure, undo and `new_fields` addition SHALL write one shape-history entry for its collection. A create's entry is the collection's first: it holds no steps and no inverse, so it has no undo, and the daily automatic-restructure bound does not count it. The entry SHALL hold the time, the steps, the inverse steps, the `why` and the origin; an undo's entry SHALL also name the entry it undoes. Every read of an entry SHALL add its undo call, built from the entry's id.

#### Scenario: A later session sees what changed and how to undo it
- **WHEN** an agent merges two collections automatically and a fresh session reads the inventory
- **THEN** the source's history shows the merge, its origin `automatic`, its `why` and its undo call, and its steps name the target

### Requirement: An entry records who decided the change
The origin SHALL be one of a closed set: `automatic`, `user_choice`, `user_design` or `declined`. A `declined` entry SHALL record a proposal that the user refused and SHALL change nothing; `restructure` with `origin: declined` SHALL write it. A caller SHALL NOT name `automatic` as the origin: the server sets it for its own writes, including a restructure that passes the bounds of `automatic: true`.

#### Scenario: A declined proposal is kept
- **WHEN** the user answers no to a proposed new detail and the agent records it with `origin: declined`
- **THEN** the collection's shape is unchanged, and its history shows the declined proposal

### Requirement: An entry's origin follows who described or approved the shape
A create or restructure that applies a shape the user described SHALL record origin `user_design`, and one that the user approved in answer to a question SHALL record `user_choice`. A `create` SHALL take `origin` as `user_design` or `user_choice` (default `user_choice`); the server's own automatic create records `automatic`, and a caller SHALL NOT name it.

#### Scenario: A described shape records the user's design
- **WHEN** the user describes how a list is kept and the agent creates it
- **THEN** the create's shape-history entry records origin `user_design`

### Requirement: Shape history is stored with the collection's own record
A store vault SHALL write the entry in the create's or restructure's transaction and SHALL bind it into the collection's history chain, so a dropped or altered entry fails verification. A file-only vault SHALL write it in the collection's activity log.

#### Scenario: A dropped entry fails verification
- **WHEN** a store's shape-history entry is dropped or altered after its commit
- **THEN** the collection's history chain fails verification

### Requirement: The inventory shows recent history and `inspect` pages the rest
The Records and Planning inventories SHALL show the 5 newest entries per collection and the total count. `inspect` SHALL page through the full history; a store SHALL read each page by keyset over its history index, so a page costs its limit and not the history's length.

#### Scenario: The inventory stays bounded
- **WHEN** a collection has 200 history entries
- **THEN** the inventory shows the 5 newest and the total of 200, and `inspect` pages through the rest

### Requirement: The served contract teaches structure management
The served Records and Planning contract SHALL teach the agent the rules of the next four requirements. The shipped scaffold and the bootstrap Records teaching SHALL carry them. Whether a change matters and whether a choice is ambiguous SHALL be the agent's judgement. No word list or threshold SHALL decide either.

#### Scenario: Each carrier holds the rules
- **WHEN** the served contract, the shipped scaffold and the bootstrap Records teaching are read
- **THEN** each teaches that a reply names the user's things in their words, and that a change of shape is surfaced only when it alters what the user gets

### Requirement: A reply keeps structure out of the user's view
A reply meant for the user SHALL name the user's things in their words and SHALL never name collections, Records, Planning, schemas, manifests, entities or vault paths. Agent-facing text MAY keep the internal names. The agent SHALL tell the user about a change of shape when it alters what they get: a new detail it will keep, lists combined or separated, a list set aside or brought back. It SHALL stay silent about reshaping that leaves what they get unchanged.

#### Scenario: A rekey stays silent
- **WHEN** the agent rekeys a collection so that a second occurrence gets its own row
- **THEN** the reply reports the new occurrence in the user's words and says nothing about keys or structure

#### Scenario: A new detail is surfaced in plain words
- **WHEN** the agent starts keeping a price for each quote
- **THEN** the reply tells the user in plain words that it now keeps the price, and names no field or collection

### Requirement: An agent asks only when the choice matters
The agent SHALL ask only when more than one reasonable shape fits and the choice changes what the user gets later. It SHALL ask one question per reply, about the user's things, with the recommended answer first. It SHALL never ask about a field, a key, a type, a collection or a schema.

#### Scenario: A conflicting merge produces one plain question
- **WHEN** two lists the agent would merge disagree about one item's value
- **THEN** the agent asks one question about that item in the user's words, recommended answer first, and merges nothing until the answer

#### Scenario: An obvious change asks nothing
- **WHEN** only one reasonable shape fits what the user said
- **THEN** the agent applies it under `silent` capture without a question

### Requirement: Advisory capture proposes and a no is remembered
Under `advisory` capture, the agent SHALL propose in plain words a change that alters what the user gets. It SHALL NOT start a change that alters nothing the user gets. A yes SHALL be applied with origin `user_choice` and a no recorded as `declined`. The agent SHALL NOT ask again about a declined proposal unless the user raises the subject.

#### Scenario: A declined proposal is not asked again
- **WHEN** the user declined a proposed new detail and the next reply touches the same subject without the user raising it
- **THEN** the agent does not propose the detail again

### Requirement: The user's own design is applied and never required
A user MAY describe a structure themselves; the agent SHALL apply it with origin `user_design` and SHALL never require it.

#### Scenario: The user's own design is applied
- **WHEN** the user describes how they want a list kept
- **THEN** the agent applies that shape with origin `user_design`, restates it in the user's words, and asks at most one question
