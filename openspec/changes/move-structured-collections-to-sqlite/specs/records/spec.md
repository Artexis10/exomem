## ADDED Requirements

### Requirement: Records is a built-in collection type and record_memory is its typed facade
`records` SHALL be a built-in collection type of kind `observed`, placement `Records`, extensible by each collection's own item schema, with default audience `policy`. `record_memory` SHALL be a typed facade: its actions SHALL map onto the generic collection operations through the built-in declaration's wire map (`append` to `add`, and every other action one to one). Its argument sets, `record_id` naming, `_record_receipt` receipts, error codes and `describe` contract SHALL be unchanged for Records collections. `record_memory` SHALL also serve collections of declared types that no facade owns, without a new tool. For those it SHALL use generic item naming, a `_collection_receipt` marker and generic error codes. The added surface SHALL be exactly two things: an `action` value `transition`, which takes the existing `item_key`, `expected_item_version`, `why` and `changes` (naming the target state), and an optional `collection_type` argument on `describe`, which teaches a named type from its declaration. The generated `record_memory` schema SHALL grow by no more than 400 bytes. It SHALL refuse Planning collections.

#### Scenario: Records wire is unchanged by the facade
- **WHEN** the same append, update and query are issued against a Records collection before and after the facade is introduced
- **THEN** arguments, receipts, error codes and query envelopes are identical in shape

#### Scenario: A declared type's item is added through record_memory
- **WHEN** an agent appends to a collection of a declared `recipes` type and later transitions it to `current`
- **THEN** both commit through the generic operations with `_collection_receipt` receipts, and the transition is checked against the declared state machine

#### Scenario: The added surface stays within its byte budget
- **WHEN** the local `record_memory` tool schema is generated before and after declared-type support
- **THEN** the only differences are the `transition` action value and the optional `collection_type` argument, and the schema grows by at most 400 bytes


### Requirement: Refused Record writes are held, inspectable and resumable
When a Records append or update refuses for a candidate-content reason (representability, undeclared field, schema type or enum failure), the writer SHALL by default preserve the complete candidate (item values, body, changes and rationale) as one held candidate in the collection store, distinct from items, with the machine-readable diagnostics attached. It SHALL render the held candidate as a read-only view under the collection's `Held/` directory, and SHALL return the held reference beside the unchanged refusal. Held candidates SHALL NOT be counted as items, returned by queries, opened by recall, or recorded as item transitions, and holding SHALL NOT advance the collection generation or audit head. The caller SHALL be able to resume a held candidate by reference through the same action with optional field overrides; a successful resume commits exactly one item and removes the held candidate in the same store transaction, and a repeated refusal updates the held diagnostics in place under the same reference. The caller SHALL be able to discard a held candidate by reference with a rationale. A caller MAY decline holding for one call. If the hold itself cannot be written, the original refusal SHALL be returned unchanged with a warning and no second error. Held candidates, including held view corrections, SHALL be subject to the same release filtering as items before they are disclosed or counted.

#### Scenario: Refusal holds the candidate
- **WHEN** an append refuses for an undeclared field
- **THEN** the response carries the refusal, the field-addressed diagnostics and a held reference, a held candidate exists in the store with a read-only view outside the item source, and the collection's item count, query results, recall candidates and audit head are unchanged

#### Scenario: Resume by reference commits once
- **WHEN** the caller resumes the held reference with an override that removes the undeclared field
- **THEN** exactly one item is committed, one transition is recorded, the held candidate and its view are removed, and inspection reports zero held candidates

#### Scenario: Repeated refusal re-holds in place
- **WHEN** a resumed candidate refuses again
- **THEN** the same held reference remains with updated diagnostics and no second held candidate is created

#### Scenario: Declined hold refuses plainly
- **WHEN** the caller declines holding and the append refuses
- **THEN** no held candidate is written and the refusal is returned with its field-addressed details

#### Scenario: Discard removes the candidate
- **WHEN** the caller discards a held reference with a rationale
- **THEN** the held candidate and its view are removed, the audit head is unchanged, and inspection no longer lists it

#### Scenario: Hold failure does not mask the refusal
- **WHEN** the held candidate cannot be written
- **THEN** the original refusal is returned with a warning and without a held reference

#### Scenario: Withheld candidate is invisible
- **WHEN** release rules would withhold the candidate's values from the requesting audience
- **THEN** inspection and inventory report neither its reference nor its count to that audience

#### Scenario: Editing a held view changes nothing
- **WHEN** a user edits a held candidate's view
- **THEN** the held candidate is unchanged and the view is re-rendered

## MODIFIED Requirements

### Requirement: Records accept a governed bulk upsert under one guard
`record_memory` SHALL accept `action: "bulk_upsert"` with `collection`, `why`, required `expected_container_hash`, and `rows` of `item`, optional `body` and optional `source`. It SHALL accept optional batch `source` and `on_reject` of `abort` (default) or `skip`. File mode SHALL accept 1 to 50 rows; store mode SHALL accept 1 to 500 rows.

The action SHALL hold one writer-lease mutation guard and compare the container hash once against one collection snapshot. It SHALL plan every row against that snapshot. File mode SHALL publish item files, the manifest audit head and log entries in one atomic batch. Store mode SHALL commit canonical rows, versions, sources, effects, pending views and one transaction receipt together.

Both modes SHALL refuse `dataset` and a batch that exceeds the collection's item capacity before writing. The capacity refusal SHALL use `COLLECTION_ITEM_LIMIT` and report the current count, additions and maximum. File mode SHALL also refuse `BULK_UPSERT_AUDIT_DEPTH` if per-item events would exceed the 2048-event audit depth budget. That refusal SHALL name events used, events needed and the budget. Store mode SHALL apply no file audit-depth budget.

Each row SHALL be validated as a single append is validated (schema, representability, undeclared fields, value and body limits) with every failing field path named. Row identity SHALL derive from the declared natural key when a row's values complete it. A row without a complete natural key SHALL NOT be refused: bulk SHALL give it a generated identity and report `inserted` or `rejected` but never `updated` or `unchanged`, each row SHALL report its identity kind (`natural-key` or `generated`), and the response and `describe` SHALL state that re-running such rows duplicates them. A row whose identity is absent SHALL be `inserted`; one whose identity is held with an identical payload SHALL be `unchanged` and write nothing; one whose identity is held with a different payload SHALL be `updated`, its values replaced wholesale and its body replaced only when the row supplies one. A row whose natural key is held under another identity, whose record is ambiguous, whose values are invalid, whose provenance fails, or which repeats an earlier row's natural-key identity in the same request SHALL be `rejected` with a code and field paths. The response SHALL carry one outcome per input row in input order, per-outcome counts, a `committed` boolean and the batch id, and SHALL never leave a row's outcome unstated.

#### Scenario: Twenty-four rows commit under one guard
- **WHEN** a file-mode client submits 24 valid rows for a collection with a declared natural key with the collection's current container hash
- **THEN** all 24 are `inserted` in one call, 24 chained audit events and one combined log write are added, the audit head is the last event, and the response's new container hash is the hash of the resulting item set
- **AND** the client needs no intermediate read between rows

#### Scenario: A stale guard refuses the whole batch
- **WHEN** `expected_container_hash` does not equal the collection's current hash
- **THEN** the call refuses with the container-hash mismatch, no row is planned or written, and the audit head is unchanged

#### Scenario: Re-submitting the same rows is unchanged
- **WHEN** a committed batch is submitted again with the refreshed container hash
- **THEN** every row is `unchanged`, no file is written and the audit head does not advance

#### Scenario: A changed row updates and does not conflict
- **WHEN** a row restates an existing natural key with one field changed
- **THEN** that row is `updated`, the other rows are unaffected, and the refusal a single append gives for a different payload (`RECORD_ID_CONFLICT`) is not raised

#### Scenario: Natural-key twin under another identity is rejected
- **WHEN** a row's natural key is already held under a different identity
- **THEN** that row is `rejected` with the natural-key conflict code, and in `abort` mode nothing is written

#### Scenario: Duplicate identity inside one request
- **WHEN** two rows in one request derive the same identity
- **THEN** the later row is `rejected` `DUPLICATE_ROW_KEY` naming the earlier row's index, and neither silently overwrites the other

#### Scenario: Rows without a complete natural key insert only
- **WHEN** rows whose values do not complete the declared natural key are submitted, and then the same rows again
- **THEN** the first call reports every row `inserted` with `identity: "generated"`, the second inserts them again, and no row is ever `updated` or `unchanged`

#### Scenario: Oversized batch refuses
- **WHEN** a file-mode request carries 51 rows
- **THEN** it refuses whole with the typed `BULK_UPSERT_TOO_MANY_ROWS` error, whose message and details say to split the import into batches of at most the cap chained by the returned `after_container_hash` (`rows`, `maximum`, `batches_needed`, `chain_with`), and no file, manifest change or audit entry is written

#### Scenario: A batch past the item ceiling refuses
- **WHEN** a request within its mode's row cap would raise the collection's item count past `COLLECTION_ITEM_LIMIT`
- **THEN** it refuses whole with `COLLECTION_ITEM_LIMIT` naming the items held, the items to add and the maximum, and nothing is written

#### Scenario: A batch that would cross the chain-depth budget refuses up front
- **WHEN** the file-mode collection's audit chain already holds 2040 events and a batch would write 20 items
- **THEN** the call refuses `BULK_UPSERT_AUDIT_DEPTH` naming events used, events needed and the budget, and nothing is written

#### Scenario: Five hundred store rows share one transition
- **WHEN** a store-mode client submits 500 valid rows with the current container hash
- **THEN** one SQLite transaction commits the rows, their versions, verified sources and per-row audit effects
- **AND** one transition advances the collection generation, every written outcome names that transition, and no file audit-depth check applies

#### Scenario: An oversized store batch refuses
- **WHEN** a store-mode request carries 501 rows
- **THEN** it refuses `BULK_UPSERT_TOO_MANY_ROWS` with a maximum of 500 and no canonical or projection change

### Requirement: Bulk upsert is atomic or reports partial without ambiguity
In `abort` mode, if any row is `rejected` the action SHALL write nothing, SHALL report `committed: false`, and SHALL still report every row's would-be outcome so the caller can correct all failures in one round trip. In `skip` mode the accepted rows SHALL be published in one atomic batch and the rejected rows reported, `committed` SHALL be true only when that batch was published, and a failure before commit SHALL roll back the whole batch. A batch with no `inserted` or `updated` row SHALL write nothing and SHALL NOT advance the audit head. No response or crash SHALL leave some rows durable and others reported failed within one committed batch.

#### Scenario: Abort mode writes nothing on one bad row
- **WHEN** one of 24 rows carries an undeclared field and `on_reject` is `abort`
- **THEN** no item, manifest change or audit entry is written, `committed` is false, the bad row is `rejected` naming its field path, and the other 23 rows report the outcome they would have had

#### Scenario: Skip mode commits the accepted rows once
- **WHEN** the same file-mode request is made with `on_reject: "skip"`
- **THEN** the 23 accepted rows are published in one atomic batch, 23 chained events are written, `committed` is true and the bad row is `rejected` with its field paths

#### Scenario: A publication failure leaves nothing behind
- **WHEN** the file-mode atomic publication or the store-mode canonical transaction fails before commit
- **THEN** no planned item, manifest change or log entry remains, and the caller may retry the identical request

#### Scenario: Store skip mode commits the accepted rows once
- **WHEN** one of 24 store-mode rows is invalid and `on_reject` is `skip`
- **THEN** one transaction commits the 23 accepted rows and their effects, and reports the rejected row with its field paths

#### Scenario: Projection failure preserves a committed store batch
- **WHEN** a store batch commits but a view installation fails afterward
- **THEN** the receipt reports the committed batch with a projection warning, and retry adds no duplicate mutation

### Requirement: Bulk upsert leaves one batch receipt over per-item chained events
File mode SHALL retain one ordinary chained event per written item, in input order. The event SHALL use `append` for an inserted item and `update` for an updated item. Unchanged and rejected rows SHALL write no event. Each event SHALL chain from the preceding event with continuous container and manifest hashes. One atomic publication SHALL write the item files, final manifest audit head and combined `log.md` entries. Each item SHALL carry its event's transition marker. Each event's rationale SHALL include `why`, batch identity and row position. Without a declared `sources` link field, the event SHALL also record the verified source reference.

Store mode SHALL record one `bulk_upsert` transaction and one audit effect per written item, in input order. Each effect SHALL retain the item's before and after versions and hashes, with verified provenance in canonical source rows. All written outcomes SHALL name that one transition. The first and last transition fields SHALL equal it. Store mode SHALL write no file audit marker or `log.md` entry.

Both modes SHALL return one batch receipt with the batch identity, row count, outcome counts, commit state and ordered row outcomes. Each written outcome SHALL include its transition identity and verified source reference. Transaction records, audit effects and receipts SHALL contain no row values. No new idempotency argument SHALL be added. A retry under the same transport identity SHALL return the recorded result without another write. Without transport identity, a refreshed naturally keyed batch SHALL report already committed rows as unchanged.

#### Scenario: One batch receipt over chained events
- **WHEN** a file-mode batch of 24 inserts commits
- **THEN** the response carries one batch receipt with 24 per-row outcomes, exactly 24 chained events verify as one continuous chain ending at the manifest audit head, each item's marker names its own event, and the audit chain inspects as complete

#### Scenario: Provenance without a sources field is durable in the chain
- **WHEN** file-mode rows are committed to a collection that declares no `sources` link field
- **THEN** every row is written and each event's rationale names its verified source reference

#### Scenario: Retry after a lost response
- **WHEN** the caller retries the identical request under the same transport idempotency identity after the response was lost
- **THEN** the recorded result is returned, no second event is written and no file changes

#### Scenario: Keyless retry on a natural-keyed collection
- **WHEN** the identical request is sent again with no transport identity and the refreshed container hash
- **THEN** every row is `unchanged` and no event is written

#### Scenario: Depth cannot be evaluated
- **WHEN** file-mode log history is not fully parsed (a size, event-count or segment ceiling, or a malformed line) so the collection's used depth cannot be counted
- **THEN** the call is not refused for depth, and the result carries `depth_check: "unknown"` with `depth_check_reason: "history_incomplete"`; when the history parses, the exact check applies and no marker is added

#### Scenario: Store provenance survives a batch without a sources field
- **WHEN** a store batch commits to a collection without a `sources` link field
- **THEN** canonical source rows retain every written item's verified reference and the receipt reports provenance by input row

#### Scenario: A store batch leaves file audit history untouched
- **WHEN** a store batch commits multiple item changes
- **THEN** one transaction and its per-item effects extend canonical history, without reading or rewriting `log.md`

### Requirement: Describe teaches bulk upsert
`record_memory(action="describe")` SHALL teach the row shape, container guard, four outcomes, `abort` versus `skip`, provenance and retries. It SHALL state the active mode's row cap and explain chaining larger imports through the returned container hash. For file mode it SHALL teach the 50-row cap, writer-lease cost and 2048-event audit depth budget. For store mode it SHALL teach the 500-row cap, one canonical transition and the absence of that file depth budget. It SHALL explain that bulk updates changed payloads, whereas append refuses an identity conflict. It SHALL explain generated identities for incomplete natural keys and duplication on repeated calls without transport identity. Examples SHALL use invented fields without personal or product identity.

#### Scenario: A generic client bulk-loads from describe alone
- **WHEN** a client authors a batch only from the `describe` example against a fresh sample collection
- **THEN** the batch is accepted without a guessed field, code or limit
