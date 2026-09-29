## ADDED Requirements

### Requirement: Records accept a governed bulk upsert under one guard
`record_memory` SHALL accept `action: "bulk_upsert"` taking `collection`, `why`, `expected_container_hash` (required), `rows` (1 to 500 objects of `item`, optional `body` and optional `source`), an optional batch `source`, an optional `on_reject` of `abort` (default) or `skip`, and an optional `idempotency_key`. The action SHALL run inside one writer-lease mutation guard, compare `expected_container_hash` once against one read of the collection, plan every row against that one snapshot, and publish all planned files, the manifest audit head and the log entry in one atomic batch. It SHALL refuse a collection without a complete declared natural key, and the `dataset` strategy, and SHALL refuse a batch whose projected item count would exceed the collection's item ceiling or whose audit size would exceed its caps, before planning any row.

Each row SHALL be validated as a single append is validated (schema, representability, undeclared fields, value and body limits) with every failing field path named. Row identity SHALL derive from the declared natural key. A row whose identity is absent SHALL be `inserted`; one whose identity is held with an identical payload SHALL be `unchanged` and write nothing; one whose identity is held with a different payload SHALL be `updated`, its values replaced wholesale and its body replaced only when the row supplies one. A row whose natural key is held under another identity, whose record is ambiguous, whose values are invalid, whose provenance fails, or which repeats an earlier row's identity in the same request SHALL be `rejected` with a code and field paths. The response SHALL carry one outcome per input row in input order, per-outcome counts, a `committed` boolean and the audit correlation, and SHALL never leave a row's outcome unstated.

#### Scenario: Twenty-four rows commit under one guard
- **WHEN** a client submits 24 valid rows for a collection with a declared natural key with the collection's current container hash
- **THEN** all 24 are `inserted` in one call, exactly one audit transition and one log entry are added, the audit head advances once, and the response's new container hash is the hash of the resulting item set
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

#### Scenario: Oversized batch refuses
- **WHEN** a request carries 501 rows, or would raise the collection past its item ceiling
- **THEN** it refuses whole with a bounded-size code before any row is validated for writing

### Requirement: Bulk upsert is atomic or reports partial without ambiguity
In `abort` mode, if any row is `rejected` the action SHALL write nothing, SHALL report `committed: false`, and SHALL still report every row's would-be outcome so the caller can correct all failures in one round trip. In `skip` mode the accepted rows SHALL be published in one atomic batch and the rejected rows reported, `committed` SHALL be true only when that batch was published, and a failure during publication SHALL roll back the whole batch. A batch with no `inserted` or `updated` row SHALL write nothing and SHALL NOT advance the audit head. No response or crash SHALL leave some rows durable and others reported failed within one committed batch.

#### Scenario: Abort mode writes nothing on one bad row
- **WHEN** one of 24 rows carries an undeclared field and `on_reject` is `abort`
- **THEN** no item, manifest change or audit entry is written, `committed` is false, the bad row is `rejected` naming its field path, and the other 23 rows report the outcome they would have had

#### Scenario: Skip mode commits the accepted rows once
- **WHEN** the same request is made with `on_reject: "skip"`
- **THEN** the 23 accepted rows are published in one atomic batch, one audit transition is written, `committed` is true and the bad row is `rejected` with its field paths

#### Scenario: A publication failure leaves nothing behind
- **WHEN** the atomic publication fails part way
- **THEN** no planned item, manifest change or log entry remains, and the caller may retry the identical request

### Requirement: Every bulk row carries verified provenance
Each row SHALL name a preserved Evidence or Source page through `source` or the batch `source`. The reference SHALL be resolved through the ordinary reader and the requesting audience's release filter at planning, and re-guarded at publication. A row with no reference SHALL be `rejected` `SOURCE_REQUIRED`. A reference that does not exist and one that is withheld from the audience SHALL produce an identical rejection (`SOURCE_NOT_FOUND`) with identical details. The reference SHALL be recorded on the row through the collection's declared link-array `sources` field, appended when absent; a collection with no such field SHALL refuse `bulk_upsert` rows as `SOURCE_FIELD_UNDECLARED`.

#### Scenario: Every row points at preserved Evidence
- **WHEN** 24 rows are committed with a batch `source` naming a preserved Evidence page
- **THEN** each written item's `sources` field links that page

#### Scenario: Withheld source reads as absent
- **WHEN** one row names a page that the requesting audience may not read and another names a page that does not exist
- **THEN** both rows are rejected with the same code and details, and nothing distinguishes them

### Requirement: Bulk upsert leaves one receipt and is idempotent on retry
A committed batch SHALL append exactly one audit transition (`operation: "bulk_upsert"`) to the collection's audit chain and exactly one log entry. The transition SHALL carry the ordered batch payload hash, the row count, the count per outcome, the `idempotency_key` when supplied, and the before and after container hashes, and SHALL NOT carry row values. Each written item SHALL carry the transition id in its own audit marker. A retry with the same `idempotency_key` and the same payload SHALL return the recorded result with `replayed: true` and write nothing, even after the container hash has changed; the same key with a different payload SHALL refuse `IDEMPOTENCY_KEY_REUSED`. A retry without a key SHALL be safe because every already-committed row is `unchanged`.

#### Scenario: One receipt for the batch
- **WHEN** a batch commits
- **THEN** exactly one transition and one log entry name it, they carry counts and hashes but no row values, and each written item's marker names that transition

#### Scenario: Retry after a lost response
- **WHEN** the caller retries the identical request with the same idempotency key after the response was lost
- **THEN** the recorded result is returned with `replayed: true`, no second transition is written and no file changes

#### Scenario: Key reuse with different rows refuses
- **WHEN** the same key is sent with a different row set
- **THEN** the call refuses `IDEMPOTENCY_KEY_REUSED` and writes nothing

### Requirement: Bulk upsert inherits exactly-once and withheld-as-absent
A batch SHALL be authorised over the union of every path it would touch before any write, and SHALL commit exactly once or leave no file, manifest change or audit entry. A natural-key collision with an item the audience may not read SHALL be reported as `rejected` with the natural-key conflict code and no item references, and no outcome, count or echo SHALL reveal the existence or content of an item the audience may not read.

#### Scenario: Hidden twin is not disclosed
- **WHEN** a row's natural key matches an item withheld from the requesting audience
- **THEN** the row is `rejected` with no `item_keys`, is not reported as `updated` or `unchanged`, and no value from the withheld item appears anywhere in the response

#### Scenario: Crash between commit and response
- **WHEN** the response is lost after the batch committed
- **THEN** a retry, with or without the key, adds no second transition and no duplicate item

### Requirement: Describe teaches bulk upsert
`record_memory(action="describe")` SHALL document `bulk_upsert`: the row shape, the 500-row bound, the required natural key and container-hash guard, the four outcomes, `abort` versus `skip`, provenance, idempotency, and that a changed payload for a held identity is `updated` (unlike `append`), using a generic example with invented field names and no personal or product identity.

#### Scenario: A generic client bulk-loads from describe alone
- **WHEN** a client authors a batch only from the `describe` example against a fresh sample collection
- **THEN** the batch is accepted without a guessed field, code or limit

#### Scenario: Frozen hosted candidates are unchanged
- **WHEN** the v1, v2 and v3 Claude hosted candidate schemas are compared with their released digests
- **THEN** they are byte-identical, and only the local surface, the v5 candidate and the command binding list `bulk_upsert`
