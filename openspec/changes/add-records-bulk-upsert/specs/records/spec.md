## ADDED Requirements

### Requirement: Records accept a governed bulk upsert under one guard
`record_memory` SHALL accept `action: "bulk_upsert"` taking `collection`, `why`, `expected_container_hash` (required), `rows` (1 to 500 objects of `item`, optional `body` and optional `source`), an optional batch `source` and an optional `on_reject` of `abort` (default) or `skip`. The action SHALL run inside one writer-lease mutation guard, compare `expected_container_hash` once against one read of the collection, plan every row against that one snapshot, and publish all planned files, the manifest audit head and the log entries in one atomic batch. It SHALL refuse the `dataset` strategy, and SHALL refuse, before any write and with no partial effect, a batch whose projected item count would exceed the collection's item ceiling (`COLLECTION_ITEM_LIMIT`) or whose per-item audit events would carry the collection's audit chain past its 2048-event depth budget (`BULK_UPSERT_AUDIT_DEPTH`, naming the events used, the events needed and the budget).

Each row SHALL be validated as a single append is validated (schema, representability, undeclared fields, value and body limits) with every failing field path named. Row identity SHALL derive from the declared natural key. A collection without a complete declared natural key SHALL NOT be refused: bulk SHALL run insert-only there, giving each row a generated identity and reporting `inserted` or `rejected` but never `updated` or `unchanged`, and the response and `describe` SHALL state that re-running the same rows duplicates them. A row whose identity is absent SHALL be `inserted`; one whose identity is held with an identical payload SHALL be `unchanged` and write nothing; one whose identity is held with a different payload SHALL be `updated`, its values replaced wholesale and its body replaced only when the row supplies one. A row whose natural key is held under another identity, whose record is ambiguous, whose values are invalid, whose provenance fails, or which repeats an earlier row's natural-key identity in the same request SHALL be `rejected` with a code and field paths. The response SHALL carry one outcome per input row in input order, per-outcome counts, a `committed` boolean and the audit correlation, and SHALL never leave a row's outcome unstated.

#### Scenario: Twenty-four rows commit under one guard
- **WHEN** a client submits 24 valid rows for a collection with a declared natural key with the collection's current container hash
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

#### Scenario: No natural key runs insert-only
- **WHEN** a collection with no complete declared natural key receives a valid batch, and then the same batch again
- **THEN** the first call reports every row `inserted` with `identity: "generated"`, the second inserts them again, and no row is ever `updated` or `unchanged`

#### Scenario: Oversized batch refuses
- **WHEN** a request carries 501 rows, or would raise the collection past its item ceiling
- **THEN** it refuses whole with a bounded-size code and no file, manifest change or audit entry is written

#### Scenario: A batch that would cross the chain-depth budget refuses up front
- **WHEN** the collection's audit chain already holds 2040 events and a batch would write 20 items
- **THEN** the call refuses `BULK_UPSERT_AUDIT_DEPTH` naming events used, events needed and the budget, and nothing is written

### Requirement: Bulk upsert is atomic or reports partial without ambiguity
In `abort` mode, if any row is `rejected` the action SHALL write nothing, SHALL report `committed: false`, and SHALL still report every row's would-be outcome so the caller can correct all failures in one round trip. In `skip` mode the accepted rows SHALL be published in one atomic batch and the rejected rows reported, `committed` SHALL be true only when that batch was published, and a failure during publication SHALL roll back the whole batch. A batch with no `inserted` or `updated` row SHALL write nothing and SHALL NOT advance the audit head. No response or crash SHALL leave some rows durable and others reported failed within one committed batch.

#### Scenario: Abort mode writes nothing on one bad row
- **WHEN** one of 24 rows carries an undeclared field and `on_reject` is `abort`
- **THEN** no item, manifest change or audit entry is written, `committed` is false, the bad row is `rejected` naming its field path, and the other 23 rows report the outcome they would have had

#### Scenario: Skip mode commits the accepted rows once
- **WHEN** the same request is made with `on_reject: "skip"`
- **THEN** the 23 accepted rows are published in one atomic batch, 23 chained events are written, `committed` is true and the bad row is `rejected` with its field paths

#### Scenario: A publication failure leaves nothing behind
- **WHEN** the atomic publication fails part way
- **THEN** no planned item, manifest change or log entry remains, and the caller may retry the identical request

### Requirement: Every bulk row carries verified provenance
Each row SHALL name a preserved Evidence or Source page through `source` or the batch `source`. The reference SHALL be resolved through the ordinary reader and the requesting audience's release filter at planning, and re-guarded at publication. A row with no reference SHALL be `rejected` `SOURCE_REQUIRED`. A reference that does not exist and one that is withheld from the audience SHALL produce an identical rejection (`SOURCE_NOT_FOUND`) with identical details. When the collection declares a link-array `sources` field the verified reference SHALL be appended to it when absent. When it declares none the row SHALL NOT be rejected, and the verified reference SHALL be recorded per row index in the audit receipt instead.

#### Scenario: Every row points at preserved Evidence
- **WHEN** 24 rows are committed with a batch `source` naming a preserved Evidence page
- **THEN** each written item's `sources` field links that page

#### Scenario: No sources field records provenance in the receipt
- **WHEN** rows are committed to a collection that declares no `sources` link field
- **THEN** every row is written, and the audit receipt lists each row index with its verified source reference

#### Scenario: Withheld source reads as absent
- **WHEN** one row names a page that the requesting audience may not read and another names a page that does not exist
- **THEN** both rows are rejected with the same code and details, and nothing distinguishes them

### Requirement: Bulk upsert leaves one batch receipt over per-item chained events
The audit protocol SHALL be unchanged: a committed batch SHALL append one ordinary chained audit event per written item (`append` for `inserted`, `update` for `updated`; `unchanged` and `rejected` rows write none), in input order, each parented on the previous, with continuous before and after container and manifest hashes, all published in ONE atomic write together with the item files, the final manifest audit head and one combined `log.md` write carrying one entry per event. Each written item SHALL carry its own event's transition id in its audit marker. Each event's rationale SHALL be the caller's `why` followed by the batch id and the row's position, and, when the collection declares no `sources` link field, by the row's verified provenance reference. The response SHALL carry ONE batch receipt: the batch id, the row count, the count per outcome, `committed`, the first and last transition ids, and the per-row outcomes with each written row's transition id and provenance reference. No event or receipt SHALL carry row values. `bulk_upsert` SHALL add no idempotency argument: a retry under the same transport idempotency identity SHALL return the recorded result without a second write, and on a naturally keyed collection a retry without one SHALL be safe because every already-committed row is `unchanged`.

#### Scenario: One batch receipt over chained events
- **WHEN** a batch of 24 inserts commits
- **THEN** the response carries one batch receipt with 24 per-row outcomes, exactly 24 chained events verify as one continuous chain ending at the manifest audit head, each item's marker names its own event, and the audit chain inspects as complete

#### Scenario: Provenance without a sources field is durable in the chain
- **WHEN** rows are committed to a collection that declares no `sources` link field
- **THEN** every row is written and each event's rationale names its verified source reference

#### Scenario: Retry after a lost response
- **WHEN** the caller retries the identical request under the same transport idempotency identity after the response was lost
- **THEN** the recorded result is returned, no second event is written and no file changes

#### Scenario: Keyless retry on a natural-keyed collection
- **WHEN** the identical request is sent again with no transport identity and the refreshed container hash
- **THEN** every row is `unchanged` and no event is written

### Requirement: Bulk upsert inherits exactly-once and withheld-as-absent
A batch SHALL be authorised over the complete collection item set and every path it would touch before any write, exactly as a single append is, and SHALL commit exactly once or leave no file, manifest change or audit entry. When any manifest, source or item path of the collection is withheld from the requesting audience the call SHALL refuse `COLLECTION_NOT_FOUND`, indistinguishable from an absent collection, before any row is planned, so no outcome, count or echo can reveal the existence or content of an item the audience may not read.

#### Scenario: A withheld item makes the collection read as absent
- **WHEN** any item of the collection is withheld from the requesting audience
- **THEN** the call refuses `COLLECTION_NOT_FOUND`, identical to an absent collection, and nothing about the withheld item appears in the response or on disk

#### Scenario: Crash between commit and response
- **WHEN** the response is lost after the batch committed
- **THEN** a retry, with or without a transport identity, adds no second event and no duplicate item on a naturally keyed collection

### Requirement: Describe teaches bulk upsert
`record_memory(action="describe")` SHALL document `bulk_upsert`: the row shape, the 500-row bound, the container-hash guard, the four outcomes, `abort` versus `skip`, provenance, retry behaviour, the 2048-event audit chain-depth budget and that each written row consumes one event of it, that a changed payload for a held identity is `updated` (unlike `append`), and that a collection without a natural key runs insert-only so re-runs duplicate, using a generic example with invented field names and no personal or product identity.

#### Scenario: A generic client bulk-loads from describe alone
- **WHEN** a client authors a batch only from the `describe` example against a fresh sample collection
- **THEN** the batch is accepted without a guessed field, code or limit

#### Scenario: Frozen hosted candidates are unchanged
- **WHEN** the v1, v2 and v3 Claude hosted candidate schemas are compared with their released digests
- **THEN** they are byte-identical, and only the local surface, the v5 candidate and the command binding list `bulk_upsert`
