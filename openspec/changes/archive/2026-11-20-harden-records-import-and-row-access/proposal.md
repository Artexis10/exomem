## Why

A real import of a few thousand structured records showed that Records cannot yet keep its promise to ingest, query and update such a collection reliably and quickly. On 2026-11-15 a 4,795-row import into a store collection took 21 minutes and ran into four failures.

- **Wrong rows were admitted.** A `required` integer field that is part of the natural key arrived as `null` for 734 rows. All 734 hashed to one natural key that contained `null`. One row was inserted, 733 were counted as duplicates and none was rejected. The preview checked only the first 100 rows, so it could not see the tail.
- **The import blocked other work.** Each 500-row batch held the vault write boundary for its whole wall time, up to 182 s. Unrelated writes were refused `MUTATION_BUSY`, and import status was refused `COLLECTION_STORE_BUSY`. One batch needs about 0.3 s of CPU. It took minutes because a batch issues about nine SQLite statements per row, and each statement gives up the GIL to the CPU-bound passes that the service runs in the same process. With two such threads, a 0.3 s batch takes 32-39 s.
- **Edit by row id failed.** `edit_record` with a row id found the row by paging the whole collection through the legacy query action. On a 755-row collection, the first page exceeded the 64 KiB response cap, and the edit was refused before it wrote.
- **Filtered queries timed out.** A filtered or sorted `query_records` decoded every row of the collection in Python under a fixed 200 ms deadline. It timed out on a 154-row collection while no import was running.

## What Changes

- **Validation.** A `required` field is present **and non-null**. A `null` component does not complete a natural key. A required key part that is `null` rejects the row with `SCHEMA_REQUIRED_FIELD`. An optional key part that is `null` falls back to the generated identity that an incomplete key already gets. This holds for the values that append, update, bulk upsert and import write; rows already stored stay readable.
- **Import preview reads the source before a job starts, within a hold budget.**
  - It reports the row total, the rows that would be inserted, the rejects by code with their first source coordinates, and the rows that would collapse onto a key that another row of the same source also produces.
  - It states whether its coverage is `full`, `partial` (stopped by its budget or a malformed row) or `sampled`.
- **Import counts are honest.** `duplicates` splits into `collapsed_in_source` and `restated_existing`. The job's totals equal the sum of its batch receipts plus the counts of batches that committed nothing.
- **Edit by row id resolves one row.** It never reads the rest of the collection, and its receipt carries only that row.
- **Filtered and sorted queries cost the page, not the collection.**
  - `query_records` on a store collection runs through the SQL legacy executor that `add-collection-query-engine` Q1 already delivered and proved row for row.
  - Its remaining Q1 tasks 1.7 to 1.9 complete in that change.
  - The ownership proof is taken once per request.
  - The `rendered` copy of the rows counts once against the response cap.
- **Import batches hold the boundary briefly.**
  - Each batch runs a bounded number of set-based statements, not several per row.
  - Each hold is bounded by time and ends early for a waiting interactive writer.
  - Status answers during an import without waiting for the store thread.
  - Progress reports phase, per-phase timings, the boundary wait and hold, throughput, an ETA once two batches give a rate, and whether the job can be resumed or cancelled.
- **Load target.** The `records` spec records the scale that these bounds are designed for.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `records`: non-null required values and natural keys; a budgeted import preview over the source; honest import counts; edit by row id; filtered queries bounded by the page; short import holds, available status and progress; a recorded load target.

## Impact

- **Code:**
  - `records.py` and `structured_collections.py` (validation and keys);
  - `collection_store/importer.py` (preview, counts, status, progress);
  - `collection_store/writer.py`, `history.py`, `typed_storage.py` and `native_metadata.py` (set-based batch statements);
  - `collection_refs.py` and `record_memory.py` (bind by id);
  - `record_formats.py`, `collection_store/governance.py` and `record_governance.py` (query path);
- **Stored data:** no existing row changes, and the store schema does not change. A row already stored with a `null` key part stays readable. Its governed retraction moves to the change `add-records-retraction`.
- **Tools:**
  - `edit_record` keeps its arguments.
  - `query_records` keeps its arguments and its continuation form.
  - Import status and preview gain fields; existing fields keep their meaning, except that `duplicates` becomes the sum of its two new parts.
- **Rollback:** each delivery reverts on its own. The change has no store schema change, so reverting a release is safe.
