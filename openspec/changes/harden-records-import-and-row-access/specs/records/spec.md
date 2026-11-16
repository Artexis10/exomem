## ADDED Requirements

### Requirement: Required values and natural keys are non-null
A `required` field SHALL be present and non-null. A `null` natural-key component SHALL NOT complete the natural key.

On append, update, bulk upsert and import, a written value that is `null` in a required field, including a required child of a nested object, SHALL be rejected `SCHEMA_REQUIRED_FIELD`, naming the field path. An update checks the values it writes. A row whose optional natural-key component is `null` SHALL take the generated identity that an incomplete natural key already receives. No stored natural key SHALL be written with a `null` component.

The rule SHALL apply to written values only. A row stored before it with a `null` required value or key part SHALL stay readable, and a revise or rebaseline SHALL NOT be refused for it, unless the revise newly makes that field required. An update that completes a stored row's incomplete natural key SHALL be refused when another row already holds that key.

#### Scenario: A null required key part is rejected
- **WHEN** an import with `on_invalid: skip` maps 4,061 rows with integer positions and 734 rows whose required `position` is `null`, into a collection whose natural key includes `position`
- **THEN** 4,061 rows are imported, 734 are rejected `SCHEMA_REQUIRED_FIELD` naming `position` and their source coordinates, and no row is counted as a duplicate

#### Scenario: The default policy stops at the first invalid row
- **WHEN** the same import runs with the default `on_invalid: stop`
- **THEN** the job fails `SCHEMA_REQUIRED_FIELD` at the first `null` position, keeps the batches committed before it, and stores no `null` key part

#### Scenario: A null optional key part does not collapse rows
- **WHEN** a bulk upsert submits two rows that differ in other fields and both carry `null` in an optional natural-key field
- **THEN** both are `inserted` with `identity: "generated"`, and neither is `updated`, `unchanged` or reported as a duplicate

#### Scenario: A stored null does not block other writes
- **WHEN** a collection holds a row stored earlier with `null` in a required field, and the owner revises the collection's title
- **THEN** the revision commits, and a query still returns that row

### Requirement: Import preview counts the source within its hold budget
An import `preview` SHALL apply the job's own mapping, validation and identity derivation to the rows of the source, write nothing, and report:

- `rows_total`;
- `would_insert`;
- `would_reject` by code, with the first 20 source coordinates of each code;
- `would_collapse`, the rows whose identity another row of the same source also produces, with up to 20 sample keys;
- `coverage`.

Preview runs on the collection store thread, so it SHALL stop reading when its elapsed time reaches the documented preview budget, checked between rows. Coverage SHALL be `full` when every row was read. A read that the budget or a malformed row stopped SHALL report `coverage: partial` with the rows read, the budget and its counts so far as `partial_counts`, and SHALL NOT present those counts as the source's. A preview without a valid mapping SHALL report `coverage: sampled`.

#### Scenario: A malformed tail is found before the job starts
- **WHEN** preview reads a 1.6 MB source, which holds its 734 invalid rows after 4,061 valid ones, within its budget
- **THEN** preview reports `coverage: full`, `rows_total: 4795`, `would_insert: 4061` and `would_reject` `SCHEMA_REQUIRED_FIELD: 734` with the first coordinates

#### Scenario: A source too large for the budget
- **WHEN** preview reaches its budget before the end of the source
- **THEN** it reports `coverage: partial` with the rows read and the budget, `rows_total` and every `would_*` count are `null`, and its counts so far appear only as `partial_counts`

#### Scenario: Preview and import agree
- **WHEN** a job runs over a source that preview read in full
- **THEN** the job's inserted, rejected and collapsed counts equal the preview's

### Requirement: Import counts distinguish collapse from restatement
Import status and receipts SHALL report `collapsed_in_source` for rows whose identity an earlier row of the same job produced, and `restated_existing` for rows that restated a row already stored. `duplicates` SHALL equal their sum.

A batch that commits rows SHALL write its counts in its receipt. A batch that commits no row writes no receipt, so status SHALL report its counts in `uncommitted`. For a job on a store collection, each total SHALL equal the sum of the same field over the job's batch receipts plus `uncommitted`. `uncommitted` SHALL be `null` on a derived collection and for a job that an earlier release recorded.

#### Scenario: Totals equal receipts plus the batches that committed nothing
- **WHEN** a job on a store collection finishes after batches that commit rows and a last batch that holds only rejected rows
- **THEN** its inserted, updated, unchanged, rejected, collapsed and restated totals each equal the sum of the same field over its batch receipts plus `uncommitted`, and `uncommitted` holds the last batch's rejections

### Requirement: A row id resolves one row
`edit_record` and every action that takes a row id SHALL resolve the id to one row without rendering or returning the other rows of the collection.

- On a store collection of up to 100,000 rows, for a caller who reads every field, the id SHALL resolve with one key-and-version read, filtered by the caller's read decision on each matching row. This includes a caller to whom some rows are withheld. The guards SHALL be the row's item version and the collection snapshot that the caller's query returns.
- A derived collection, a collection above 100,000 rows, a caller limited to some fields, and a collection the store does not serve SHALL resolve the id through the collection's query, in pages that each fit the response cap.

A row withheld from the caller SHALL be reported exactly as an absent row. A stale id SHALL be refused with the row's current id. The mutation receipt SHALL describe only the edited row.

#### Scenario: Editing one row of a 755-row collection
- **WHEN** a caller edits one field of a row in a 755-row store collection by the id a query returned
- **THEN** the edit commits, the receipt names that row only, and no response-size refusal occurs

#### Scenario: A withheld row is not revealed by its id
- **WHEN** a caller who cannot read a row submits that row's id
- **THEN** the refusal is identical to the refusal for an id that names no row

### Requirement: Filtered and sorted Records queries are bounded by the page
`query_records` with filters, a sort or an aggregate on a store collection SHALL evaluate them in the store, within the interactive query profile, over released rows only. Its cost SHALL depend on the rows that the filter and page touch, not on decoding every row of the collection. Its rows, order, totals, continuation and aggregates SHALL equal the legacy evaluation of the same request.

#### Scenario: A filter on a small collection does not time out
- **WHEN** a caller filters a 154-row store collection by `startswith` on a string field while background passes run
- **THEN** the matching rows return within the interactive profile, with no `QUERY_TIMEOUT`

#### Scenario: A filter on a 5,000-row collection
- **WHEN** a caller filters a 5,000-row store collection with a limit of 10
- **THEN** the page returns at p95 within 300 ms, and its rows equal the legacy evaluation of the same request

### Requirement: Import work holds the write boundary briefly and status stays available
An import batch of new rows SHALL take a number of store `execute` calls bounded by the tables it writes, not by its rows. Its fetched rows SHALL be a fixed number plus at most one per row written. A batch that updates or restates existing rows is outside this bound. An import batch SHALL close when its elapsed time reaches the documented hold budget, as well as at its row and byte limits. Between batches it SHALL end its hold for a waiting interactive writer.

Import `status` SHALL answer from a read snapshot without waiting for the collection store thread. It SHALL report:

- `phase` and the current batch;
- per-phase timings;
- the boundary wait and hold of the last and the longest batch;
- rows per second;
- an ETA once two batches give a rate;
- whether the job can be resumed and cancelled.

#### Scenario: A batch of new rows
- **WHEN** an import commits a batch of 500 new rows into a summary collection that declares no references
- **THEN** the batch takes fewer `execute` calls than it has rows, and its fetched rows are at most the rows written plus a fixed number, as the benchmark records in `executes` and `rows_fetched`

#### Scenario: An unrelated write during an import
- **WHEN** a 4,795-row import runs while background passes keep the process busy, and the agent saves an unrelated note
- **THEN** the note commits without `MUTATION_BUSY`, and no import batch holds the boundary longer than the hold budget

#### Scenario: Status during a batch
- **WHEN** a caller asks for status while a batch is running
- **THEN** status answers within 250 ms with the job's phase, counts and timings, and is never refused `COLLECTION_STORE_BUSY`

### Requirement: Records records its load target
The Records bounds SHALL hold for a store collection of up to 100,000 rows on one personal service under the interactive profile:

| Operation | Bound |
|---|---|
| Edit by id | p95 ≤ 300 ms |
| Filtered query with a limit of 100 or less | p95 ≤ 300 ms at 5,000 rows; ≤ 1 s at 100,000 rows |
| A 5,000-row import | ≤ 30 s end to end |
| Any single boundary hold by import work | p99 ≤ 0.5 s |
| Import status | p95 ≤ 250 ms while an import runs |

Derived collections keep their own bounds.

#### Scenario: The bounds are measured, not assumed
- **WHEN** a release changes the Records write, query or import path
- **THEN** the Records benchmark runs on a generated fixture of the documented shape, and its recorded results show each bound before and after the change
