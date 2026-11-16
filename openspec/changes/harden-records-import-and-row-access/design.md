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
- No governed retraction of a stored row: it needs a store schema upgrade, so it moves to the change `add-records-retraction`.

## Decisions

### 1. `null` is absent for `required` and for natural keys

A `required` field must be present and non-null. A `null` key component leaves the natural key incomplete.

- A required key part that is `null` rejects with `SCHEMA_REQUIRED_FIELD`, naming the field.
- An optional key part that is `null` takes the generated identity that the bulk contract already gives an incomplete key.

The rule lives in the one validation function that append, update, bulk upsert and import share. It is not repeated per path.

- It applies to the values a write supplies, including a required child of a nested object. An update checks the fields it changes.
- A stored row is read and re-checked as it was written. A row already stored with a `null` required value or key part stays readable, and a revise or rebaseline does not refuse it. A revise that newly makes a field required checks that field on every stored row.
- A stored natural key never holds a `null` part. An update that completes a stored row's incomplete key onto another row's key is refused as a conflict.

**Rejected alternatives:**

- Coercing `null` to a default would invent data.
- Keeping `null` as a key value is the defect itself.

### 2. Import preview streams the source through the batch's own row preparation, within a hold budget

Preview runs the same mapping, validation and key derivation as a batch, and writes nothing. It counts collapse with a set of key digests only.

- It reports `rows_total`, `would_insert`, `would_reject` by code with the first 20 source coordinates, `would_collapse` with up to 20 sample keys, and `coverage: full`.
- Preview runs on the collection store thread, and every store write and status call waits behind it. The survey costs about 0.29 s per MB uncontended: 0.69 s at 1.65 MB and 4.81 s at 16.6 MB. So preview stops at a 1 s budget (`PREVIEW_HOLD_SECONDS`), checked between rows. A stopped read reports `coverage: partial` with the rows read, the budget and its counts so far as `partial_counts`, never as the source's.
- A preview without a valid mapping reads only its sample and says `coverage: sampled`.

Using the batch's own preparation means preview and import cannot disagree.

**Rejected alternatives:**

- A larger sample still misses a tail.
- A survey off the store thread would keep full coverage, but preview is dispatched inside the store thread's call. Moving it needs a store runtime change outside this change.

### 3. Duplicates are split by cause

- `collapsed_in_source`: a later source row produced a key that an earlier row of the same job also produced.
- `restated_existing`: the row restated a row that was already stored.

`duplicates` stays the sum of the two for existing readers. The job's totals are folded from the same tally that each batch receipt carries. A batch that commits no row writes no receipt, so the job also keeps that batch's tally in `uncommitted`. On a store collection the totals then equal the receipts plus `uncommitted`.

### 4. Edit by row id binds one row without reading the collection

A row id is `<head>.<version>`, where `head` is a fixed digest prefix of the item key.

- The binder keeps a head map per collection generation: each row's head number beside its row id, about 16 bytes a row. A write starts a new generation, so the first bind after a write rebuilds the map.
- It finds the rows with the id's head, and reads their `item_key, row_version, payload_hash` in one statement. It checks the version with no rendering and no response cap.
- Each found row must pass the caller's read decision on that row (`row_release`). A withheld row reports `ITEM_NOT_FOUND`, exactly like an absent one, so the binder is not an existence oracle.
- The guards are the row's own item version and the collection snapshot that the caller's query returns. For a caller to whom some rows are withheld, that snapshot is the digest over the released rows, so the fast path also serves that caller.
- The collection's query answers instead, in pages that each fit the response cap (task 3.3), for:
  - a derived collection, whose rows the store does not hold;
  - a caller limited to some fields, who sees public item versions;
  - a collection above 100,000 rows, which keeps no head map and caches that verdict per generation;
  - a collection that the store does not serve.

The cost is one indexed range read plus one short hash per row. On a generated store, a rebuild takes about 9.5 ms at 4,061 rows and about 100 ms at 100,000 rows, inside the edit budget. A lookup scans the map in about 2 ms at 100,000 rows.

**Rejected alternatives:**

- A stored head column is a schema change for a cache.
- Changing the row id format would break every agent that holds ids.

### 5. Legacy queries on store collections select rows through the legacy executor

`add-collection-query-engine` Q1 delivered `query_engine.sqlite.execute_legacy` in PR #1563. As delivered, it ran each row through Python callbacks inside SQLite: a value decode, a match, a sort collation and a TEMP copy. CPython re-takes the GIL at every callback and statement step. Beside one busy thread, that made it slower than the whole read, at 1 to 2 s for 154 rows. Its input order also differed from the Records order on summary collections.

This change keeps `execute_legacy` as the one entry point and replaces its strategy:

- It reads only the fields that the filters, sort, date bounds and aggregate decide on. It reads them in packed keyset chunks in row-id order, one statement step per chunk.
- It decides each row with the `query_compat` primitives of the file evaluator, so the answers are equal by construction. It does not run each legacy operator in SQL.
- It returns the row ids of the page, the totals and the aggregate. The Records surface rebuilds only the returned rows. When the request names no columns, it also rebuilds the leading rows in one read and infers the default columns from them, as the file evaluator does.
- Summary rows keep their item key order. A converted row keeps its old item file path, so view path order would reorder them.
- Its deadline counts the CPU time of the request's own thread, under a fixed `records` profile of 1 s with a 2 s wall-clock backstop (`add-collection-query-engine` design §9 and §12).

The Records query keeps the whole read, under the same deadline, wherever a selected answer could differ from it: a caller limited to some fields, a derived collection, `typed-v1` rows, child tables, a profile aggregate, or a root that is a system or non-scalar field. Inside a Records query, the whole read also checks the deadline while it decodes rows, and it refuses with the same code, message and details as the selected read. Other whole summary reads bound only their fetch, as before. There is still one query engine.

- The continuation keeps its legacy offset form.
- The ownership proof is taken once per request. A routed store request hands its proof to the store thread, which proves nothing itself; only a call outside that route proves again.
- The `rendered` copy counts once against the response cap.

The Q1 phase gate's other conditions stay with that change: the 1.8 memory probe, the 1.9 gate, typed v1, declared indexes and the reader RSS envelope.

### 6. An import batch is short in statements and in time

- **Statements.** Python's sqlite3 releases the GIL at every step: an `execute` call, a parameter set of `executemany`, and a fetched row. A batch's cost under contention is its step count. The count excludes trigger programs, which run inside one step.
  - A batch of new summary rows takes a number of `execute` calls that does not grow with its rows. It reads the stored rows and natural-key holders that its rows meet with one `IN` query per 500 keys. It writes each table with chunked multi-row `INSERT` statements and reads the new row ids back with one JSON aggregate per 500 keys. It checks the event's effects with one binding query and one payload read per payload layout. Its reads of layout and field epochs run a fixed number of times, not once per row.
  - The effect check keeps its deep proof: it decodes each stored payload and recomputes its hash. That read returns one row per effect, so a batch of N new rows still takes about N fetch steps. Returning them as one row would need a lossless text form for blob and real cells, which this change does not build.
  - A batch that updates or restates existing rows still writes them one row at a time, with their per-row reads. A collection that declares references still maintains its reference index row by row. Neither is bounded by this decision.
  - The benchmark counts steps per batch on the store connection and reports `executes`, `parameter_sets`, `rows_fetched` and `steps`. It does not count through a SQLite trace callback, which also fires for trigger programs and slows the batch it counts.
- **Time.** A batch on a store collection closes at 500 rows, at 4 MiB, or when reading its rows has taken 250 ms, after at least one row. The time is measured between rows, before the batch takes the vault mutation boundary, so a process that reads rows slowly commits smaller batches. A derived collection keeps its own adaptive sizing (`derived_rows.Store.batch_rows`).
  - The budget does not time the write. Under CPU-bound threads, the write slows far more than the reading: each of its GIL-releasing steps waits for a busy thread. In the benchmark beside two such threads, 500 rows read in under 250 ms, so every batch kept 500 rows.
  - Each transaction also pays a fixed cost: a one-row batch takes 362 steps (197 `execute` calls and 165 fetched rows), and a 500-row batch takes 861. Beside two CPU-bound threads, the fixed cost alone holds the boundary for 0.78 to 0.97 s. Sizing a batch by its hold time would shrink every batch toward one row and multiply the import time, and still not bring a hold under 250 ms. A hold within the budget under contention needs that fixed cost cut first.
- **Interactive waiters.** An import tick ends after its step budget (`derived_rows.STEP_SECONDS`), at a batch boundary. While a request waits, the store thread holds the next tick until that tick starves (`StoreServer._starved`).
- **Status.** Status reads the job row through a read snapshot on the caller's own thread, over one of the store's pooled readers, inside one read transaction. It does not use the store-thread queue, so it answers while a batch runs.
  - It passes the same admission and ownership proof as a store-thread read (`StoreServer.read_beside`). A `SnapshotHandle` stands in for the writer handle and refuses every write.
  - A derived collection's open member progress is read before the snapshot, and kept only while the snapshot has not checkpointed past it.
  - Without an admitted writer in the process, or while a takeover is unresolved, status queues on the store thread as before.
  - Phase, timings, waits, holds, rate and ETA are what this process saw of the job's batches (`activity`). A job that this process did not run reports each of them as `null`.
- **Busy threads.** Beside CPU-bound threads in the process, each step that releases the GIL waits for one of them. Time then follows the step count, not the CPU work.
  - Beside two such threads, an off-thread status runs 43 statements and waits for no boundary, yet answers in a median of 2.1 to 2.6 s. A note save alone takes 25.5 s, against 0.25 s without busy threads.
  - One run each at a 0.5 ms GIL switch interval (`sys.setswitchinterval`; the default is 5 ms) took 6.5 s for the import, with a median hold of 0.6 s and a longest of 0.75 s. Status answered in a median of 0.30 s, and the note save took 3.2 s.
  - The hold and status bounds under contention need fewer steps per operation, a shorter switch interval, or the CPU-bound passes moved out of the process. This change builds none of them.

### 7. Load target

These bounds hold for a collection of up to 100,000 rows: the items-mode ceiling. They hold on one personal service with the shipped query profiles.

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
   - edit one row by id;
   - re-run a filtered query on the 154-row and 4,062-row collections.

**Rollback:** this change has no store schema change, so reverting the release is safe.
