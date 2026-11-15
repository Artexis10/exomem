## Context

Evidence comes from the 2026-11-15 import on the personal service and from a probe on `main`. The probe uses an invented source of the same shape: 58 lists, 4,795 nested rows, and a 734-row tail whose required `position` is `null`.

| Run | Result |
|---|---|
| Probe, clean temp store | 10 batches in 1.6 s; 1 row with a `null` key part inserted, 733 duplicates, 0 rejected |
| Probe, byte copy of the live store | 0.13-0.33 s per batch |
| Probe, one CPU-bound thread in the same process | 6-9 s per batch |
| Probe, two CPU-bound threads in the same process | 32-39 s per batch, with batch CPU time still under 0.5 s |
| Live service log | `bulk_upsert_records` held the vault boundary for up to 182 s per batch |

A 500-row batch issued 4,681 `execute` calls:

- `history.effects`: 1,501;
- `typed_storage.version_values`: 1,000;
- `version_layout`: 500;
- `native_metadata.read_epoch`: 500;
- `writer._item`: 500;
- `writer._twins`: 500.

Two of these return per-collection constants.

The query and edit failures come from one path. `query_records` with any filter or sort, and every page of the scan that `edit_record` uses to resolve a row id, go through `governance._summary_rows`. That function decodes every row of the collection in Python under the interactive 200 ms deadline.

Design basis:

- Anthropic, "Writing effective tools for agents": a row-addressed edit, bounded responses and actionable refusals.
- Validate tool arguments at the boundary, and name the field that fails.
- Trace one production failure to its cause, then keep the failure as a permanent regression case.

## Goals / Non-Goals

**Goals**

- No `null` required value or key part enters canonical Records.
- An import of a few thousand rows finishes in seconds, and never holds the write boundary for long.
- Edit by id and filtered queries cost the rows they touch.

**Non-goals**

- No new query engine.
- No change to file-canonical collections beyond validation.
- No multi-collection bootstrap workflow.
- No automatic cleanup of stored rows.

## Decisions

### 1. `null` is absent for `required` and for natural keys

A `required` field must be present and non-null. A `null` key component leaves the natural key incomplete.

- A required key part that is `null` rejects with `SCHEMA_REQUIRED_FIELD`, naming the field.
- An optional key part that is `null` takes the generated identity that the bulk contract already gives an incomplete key.

The rule lives in the one validation function that append, update, bulk upsert and import share. It is not repeated per path. A row already stored with a `null` key part stays readable and can be retracted.

**Rejected alternatives:**

- Coercing `null` to a default would invent data.
- Keeping `null` as a key value is the defect itself.

### 2. Import preview streams the whole source through the batch's own row preparation

Preview runs the same mapping, validation and key derivation as a batch, over every row, and writes nothing. It counts collapse with a set of key digests only.

- It reports `rows_total`, `would_insert`, `would_reject` by code with the first 20 source coordinates, `would_collapse` with up to 20 sample keys, and `coverage: full`.
- Above a 256 MiB source ceiling it samples and says `coverage: sampled` with the ceiling.

Using the batch's own preparation means preview and import cannot disagree.

**Rejected alternative:** a larger sample. It still misses a tail.

### 3. Duplicates are split by cause

- `collapsed_in_source`: a later source row produced a key that an earlier row of the same job also produced.
- `restated_existing`: the row restated a row that was already stored.

`duplicates` stays the sum of the two for existing readers. The job's totals are folded from the same tally that each batch receipt carries. A batch that commits no row writes no receipt, so the job also keeps that batch's tally in `uncommitted`. On a store collection the totals then equal the receipts plus `uncommitted`.

### 4. Edit by row id binds one row without reading the collection

A row id is `<head>.<version>`, where `head` is a fixed digest prefix of the item key.

- The binder reads only `item_key, row_version, payload_hash` for the collection in one statement. It finds the head and checks the version, with no rendering and no response cap.
- It caches the head map per collection generation. A write starts a new generation, so the first bind after a write rebuilds the map.
- The found row must pass the caller's read decision. A withheld row reports `ITEM_NOT_FOUND`, exactly like an absent one, so the binder is not an existence oracle.
- This fast path serves a caller who can read every row. A restricted caller's row version is a digest over the rows released to that caller, so the binder keeps the paged scan for that caller. Each page of the scan now fits the response cap (task 3.3).

The cost is one indexed range read plus one short hash per row. On a generated store, a rebuild takes about 9.5 ms at 4,061 rows and about 120 ms at 100,000 rows, inside the edit budget.

**Rejected alternatives:**

- A stored head column is a schema change for a cache.
- Changing the row id format would break every agent that holds ids.

### 5. Legacy queries on store collections run through the existing SQL legacy executor

`add-collection-query-engine` Q1 already delivered `query_engine.sqlite.execute_legacy`, in PR #1563. It runs every legacy operator, sort and aggregate in SQL, over an admitted collection whose released identities are streamed into a private TEMP relation. Its parity suite compares it with the Python oracle row for row. No public route calls it yet, because Q1 tasks 1.7 to 1.9 (deadline, cancellation, temp cap and the gate) are open.

This change completes Q1 1.7 to 1.9 in that change and routes `query_records` on store collections through `execute_legacy`. Store-mode `_summary_rows` then serves only reads that need whole rows. There is still one query engine, and no second path.

- The continuation keeps its legacy offset form.
- The ownership proof is taken once per request.
- The `rendered` copy counts once against the response cap.

The Q1 phase gate's other conditions stay with that change: typed v1, declared indexes and the reader RSS envelope.

### 6. Retraction is a guarded update to "not current"

`record_memory(action="retract")` takes a collection, a row id or item key, the current guards and a `why`.

- It removes the row from the current state and writes a `retract` audit effect. It keeps the row's versions.
- History shows the retraction. A later append with the same natural key inserts a new current row.
- A migration widens the `audit_effects.effect` check to admit `retract`.

Retraction follows the same governance as update. It needs write authority on the collection, and it is refused on a withheld row exactly as on an absent one.

**Rejected alternatives:**

- Hard delete loses history.
- A status field would need a schema per collection.

### 7. An import batch is short in statements and in time

- **Statements.** One batch runs set-based reads for existing rows and natural-key twins, one `executemany` per table written, and one effects check for the event. Per-collection layout and epoch are read once per batch. This bounds GIL hand-offs by the number of tables, not the number of rows.
- **Time.** A batch closes at 500 rows, at 4 MiB, or when its elapsed time reaches 250 ms. The time is measured between rows before the commit, so a contended process commits smaller batches.
- **Interactive waiters.** The existing interactive-waiter yield still ends the tick between batches.
- **Status.** Status reads the job row through the store's read snapshot on the caller's own connection, not through the store-thread queue, so it answers while a batch runs.

### 8. Load target

These bounds hold for a collection of up to 100,000 rows: the items-mode ceiling. They hold on one personal service with the interactive profile.

| Operation | Bound |
|---|---|
| Edit by id | p95 ≤ 300 ms |
| Filtered query with a limit of 100 or less | p95 ≤ 300 ms at 5,000 rows; ≤ 1 s at 100,000 rows |
| A 5,000-row import | ≤ 30 s end to end |
| Any single boundary hold by import work | p99 ≤ 0.5 s |
| Import status | p95 ≤ 250 ms while an import runs |

Derived collections keep their own path.

## Risks / Trade-offs

- **Smaller batches under contention write more transactions.** Each carries one event, so history grows faster. That is acceptable, because the verified history prefix makes the chain check incremental.
- **The binder's hash pass is linear in collection size.** It is cached, and cheap at the 100,000-row ceiling. A larger collection would need a stored head index; the ceiling makes that out of scope.

## Migration Plan

1. Release the validation, preview, counts, binder and query changes together.
2. Release the batch statements and status separately or together.
3. After installation:
   - retract the one stored row with a `null` key part;
   - verify the collection count and that no `null` key part remains, from stored state;
   - re-run the dogfood edit;
   - re-run a filtered query on the 154-row and 4,062-row collections.

**Rollback:** revert the release. The migration only widens a check, so older code still reads the store.
