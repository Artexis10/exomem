## Context

The collection store keeps every version of every Records row:

- `item_versions` holds the JSON payload of each version, and per-collection typed version tables hold the typed payloads. `typed_storage.version_values` reads either kind.
- `version_identity` binds each version to its transaction. `txns` records the actor, the `why`, the write time (`committed_at`, to the second) and the receipt. `audit_effects` links each transaction to the rows it wrote.
- These tables are append-only under triggers, and no code prunes them. A retracted row keeps all its versions. File-canonical collections keep the prior payload of each correction as a create-only file under `_Collections/history/` and prune none.
- History is hash-chained: `history.verify` rebuilds each transaction's event material and compares its hash.

`records.history` (`record_history.read`, `history.row_history`) returns one row's revisions, newest first. It takes only the internal `item_key`, and it serves only a caller who reads the whole row, because a body or a `why` can carry any field.

Most stored rows are typed. In the schema-19 rehearsal on a production store copy (archived change `add-records-retraction`, task 3.1), `item_versions` held 1,067 JSON versions against 7,157 current rows. A timeline therefore cannot rely on SQLite JSON functions alone.

The query engine already uses the name `as_of`: in a typed query it is the reference time for relative date filters. This change does not reuse that name.

Store schema is at 20. A release refuses a store whose schema is newer than its own (`COLLECTION_STORE_SCHEMA_NEWER`), and the managed supervisor refuses such a target before it stops the serving worker.

Design basis:

- R. T. Snodgrass, *Developing Time-Oriented Database Applications in SQL* (1999): valid time against transaction time, and bitemporal correction.
- ISO/IEC 9075-2:2011 application-time period tables, and XTDB's valid-time writes: a write at an earlier valid time changes the past without changing later states.
- Anthropic, "Writing effective tools for agents": few tools with clear arguments, and refusals that say what to do next.
- Agentic engineering routing, row "The model keeps misusing a tool/MCP surface": start with "Writing effective tools for agents". It is applied in D5 (one argument on an existing tool) and in the refusal codes of D3 and D7.

## Goals / Non-Goals

**Goals**

- Every field of every store-backed Records collection has a readable timeline, with no setting and no code that knows a field name.
- A write can state when its values took effect, and an unstated time is visibly the write time.
- An edit can fill in a missed past value and correct a wrong value or date, and history keeps what was believed at each time.
- A person can read history by the key they know.
- Timeline queries meet the Records load target at 100,000 rows with 5 to 20 versions per row.

**Non-goals**

- No query of the whole collection as it stood at a time (every filter, sort and aggregate evaluated in the past). The `timeline` argument reads one field.
- No query of past beliefs ("what did we think on 5 March about 1 March"). History holds the data; a query for it is a later change.
- No effective time on import, no vault time zone, and no timeline on file-canonical, dataset, derived or Planning collections.
- No future effective times. Intended future state belongs to Planning.

## Reuse check

- **Closest maintained options.** SQL:2011 application-time periods are not in SQLite. A bitemporal database such as XTDB would replace the store, its hash chain and its field governance. The SQLite R*Tree module is a virtual table: the store snapshot refuses virtual tables (`COLLECTION_SNAPSHOT_INVALID`), and its 32-bit float coordinates lose time precision.
- **Reused here.** The version spine and typed version reads; the derived-index pattern of `reference_index` (state row, `built_through`, `ready`, writer-owned batches, empty in snapshots); the query engine's `scalar_key`, which query projections already use; snapshot-bound continuations; `field_admission.FieldPlan`; the row-id binder; `history.verify`.
- **Gap that needs custom work.** Valid time with explicit corrections over governed, hash-chained versions, answered within the Records load target.
- **Added maintenance.** One timeline module (the span function and the span index), two authored tables, two derived tables and one query branch.

## Decisions

### D1. A field's timeline is a sequence of points, and a write adds a point only where it changes the field

A point says that a field held a value from an effective time. A row's first version gives every declared field a point; an absent field holds "missing". A later write adds a point to a field only where the written value differs from the field's value at the write's effective time. Each field's latest point holds the row's current value.

Options weighed:

- **Row snapshots.** Each version is a point for the whole row. A version that changes only `location` then restates the old `status`. A late-recorded status change between two versions is overwritten at the next version, although that version did not touch `status`.
- **Per-field points with change semantics** (chosen). A late fact about one field neither overwrites nor is overwritten by changes to other fields.
- **A state-ledger collection per subject** (one row per state change, as `describe` teaches today). It works now, but each collection must be designed for it, and an existing current-state collection cannot use it. It stays available.

### D2. Placement and time rules

- **Placement.** For each field, the write's effective time is compared with the field's latest point. If it is earlier, the write records a past point and the field keeps its current value. Otherwise the value becomes current. One edit can place different fields differently, and the reply names each placement.
- **Precision.** Two effective times are compared at the coarser of their precisions, and a tie counts as later. So "moved today", stated as a date after a change timed earlier the same day, becomes current, as the person means.
- **Order.** Points sort by an order instant and then by commit sequence. A date orders as 00:00 UTC of that day. A point that becomes current orders no earlier than the point it follows, so the latest point is always last.
- **Spans.** Consecutive points with the same value merge into one span. A span runs from its start to the next span's start. A span can have zero length, as when two changes share one date; it stays visible.
- **Bounds.** An instant is at most 5 minutes after its write, to allow for clock skew. A date is at most the day after the write's UTC day, so a writer ahead of UTC can name its own today.
- **Windows.** `at` with an instant returns the spans that contain it. `at` with a date returns the spans that overlap that UTC day. `from` and `to` are inclusive, and a date covers its whole day. The reply echoes the window as instants, so the interpretation is visible.

Options weighed for dates: requiring instants (rejected: people state dates, and agents would invent times); comparing a date as its 00:00 instant (rejected: a date-stated move written after a timed change on the same day would become a past point and leave the old value current); coarser-precision comparison with ordering after the point it follows (chosen). There is no vault time zone, and existing date filters compare ISO strings, so this change uses the UTC day.

### D3. Only an edit places a past point or corrects one

`edit_record` (the leaf `update`) applies the full rule: current and past placement, and `corrects`. `add_record` and bulk upsert change current state only: `effective_at` dates their current changes, and a past placement is refused `EFFECTIVE_BEFORE_CURRENT` with each field and its latest effective time.

Options weighed:

- **Every write places by its effective time.** Bulk upsert's contract says an identical payload is `unchanged` and a changed payload replaces the values. Past placement in a 50-row batch adds partial per-field outcomes and breaks that contract.
- **A separate `correct` action.** It adds a surface for what is an edit of one row.
- **Edit only** (chosen). A history can still be loaded in date order through chained bulk batches, and a missed past value is one edit.

**Corrections.** `corrects` names the transition id of the write that recorded a point. The new point replaces that point for the fields the edit names, or, when the edit names no values, for every field that point set; this re-dates a point. A correction that states no `effective_at` keeps the replaced point's effective time and basis. The replaced point leaves the timeline and stays in history. Without `corrects`, nothing leaves a timeline: a write at the same time as an earlier point adds a second point, so a correction is always explicit. A correction cannot change which point is a field's latest, other than by replacing it; such an edit is refused `TIMELINE_ORDER_CONFLICT`, and the refusal names the other point.

**Each timeline write is an ordinary update transaction with a new row version**, also when only past points change; the version's payload is then unchanged. This keeps the row's guards honest for concurrent writers, lets history find the write through its existing audit effect, and adds no audit effect kind. `row_history` today skips an update with an unchanged payload; it now keeps one that records a timeline fact.

### D4. Timeline facts live in store schema 21 and inside the hash chain

A stated effective time, a past point and a correction are authored history. They need durable storage in the write's own transaction and protection by the hash chain.

Options weighed:

- **Keys in `txns.receipt_json`** (no schema change; record receipts accept extra keys, and the `automatic` flag set the precedent). Rejected: receipts carry no row values, and a past point is a value. A receipt is also outside the event hash, so a stated time could change undetected.
- **A table that the writer creates at runtime**, as query projections do (no schema change). Rejected: an earlier release would open the store, ignore the facts and verify a chain whose hash covers them, and fail. If the hash does not cover them, authored history sits outside the chain.
- **Store schema 21** (chosen).

Migration 21 adds:

- `version_effective(row_id, row_version, effective_at)`: one row per version whose writer stated an effective time. Primary key `(row_id, row_version)`; foreign key to `version_identity`.
- `timeline_points(row_id, field_path, txn_id, row_version, placement, effective_at, value_json, corrects_txn)`: one row per past point and per correction. `placement` is `current` or `past`. `value_json` holds a past point's value and is null for a current correction, whose value is the version's. `corrects_txn` names the transaction that recorded the replaced point. A correction's `effective_at` is the stated one or, when none is stated, the replaced point's. Primary key `(row_id, field_path, txn_id)`, with an index by `txn_id`.
- `txns.timeline_version`, a nullable column that admits only `1`. It is set on a transaction that records a timeline fact. `ALTER TABLE ... ADD COLUMN` adds it without rebuilding `txns`, as migration 18 added `event_version`.
- The derived tables of D7.

Both authored tables are `STRICT` and append-only under the same triggers as `item_versions`. A defaulted effective time is never stored: it derives from `txns.committed_at`. Existing history therefore needs no backfill and reads as `written`.

When `timeline_version` is 1, the transaction's event material gains a `timeline` member: its stated facts, sorted by item key and field, with corrections named by transition id. `history.verify` rebuilds the member from the two tables. A changed, added or removed fact, or a changed marker, changes the hash. Widening the `event_version` check to a version 3 would need a rebuild of `txns`, the chain's main table, for no extra protection, so the marker column is preferred.

**Rollback cost.** Every earlier release refuses a schema-21 store. Recovery is to roll forward, or to restore the backup taken before the upgrade, which loses later writes.

### D5. One `timeline` argument on `query_records`

`query_records` gains `timeline`. `filters` keep their meaning: they select rows by current values, through the existing engine and its governance. The timeline then reads spans of one field of those rows.

Options weighed:

- **An `effective_at` that evaluates the whole query in the past.** Every filter, sort and aggregate on every field would need a historical plan: a second query engine.
- **A new tool, or a field mode on `records.history`.** A tool grows the tool list on every request. History is whole-row only by design, so it cannot serve a caller who reads only some fields.
- **One `timeline` argument returning spans of one field** (chosen). It answers the three questions asked: one row over time (filter on the row's key, no window), rows that held a value at a time or in a range (`value` with `at`, or with `from` and `to`), and rows that changed the field in a range (`changes`).

`value` filters spans by the field's value, with the filter operators. A filter on the same field in `filters` applies to its current value, so "rows now at Shelf B that stood on Shelf A on 1 March" is expressible.

### D6. History by natural key

`records.history` takes exactly one of `item_key`, `item_id` and `natural_key`.

- `natural_key` is an object of the collection's natural-key field values. The reader serializes it as the writer does and finds the current row by `(collection_id, natural_key)`. When no current row holds it, the reader derives the `hashed-v1` item key from it and finds a retracted row, whose stored natural key retraction cleared.
- `item_id` is the row id a query returns. The reader takes its key through the existing binder; a stale version still names the same row.
- A caller to whom any natural-key field is withheld cannot match a key, because each match would confirm a hidden value (`FieldPlan.natural_key_withheld`). That caller, and a caller who cannot read the row, get `RECORD_NOT_FOUND`, identical to a key that names no row.

This works on file-canonical collections too, because their history exists. It needs no schema change and ships first (D10).

### D7. A span index per field, built on demand, with a direct path for small selections

Options weighed:

- **No index; evaluate from the versions.** Typed versions decode in Python. At 100,000 rows and about 1 million versions this cannot meet the 1 s bound.
- **An eager index of every scalar field.** Every appended or imported row would write one span per field. An append-only log never changes and would pay for nothing. A 12-field collection at the load target would hold about 2.5 million spans (estimate, unverified).
- **SQLite R*Tree.** Rejected in the reuse check.
- **A B-tree span index per field, built when first asked for** (chosen). The cost falls only on fields that someone queries.

Derived tables, both listed in `DERIVED_INDEX_TABLES`, so a snapshot or replica copies them empty:

- `field_span_state(field_key INTEGER PRIMARY KEY, collection_id, field_path, encoding_hash, built_through, ready)`, unique on `(collection_id, field_path)`. `encoding_hash` covers the field's declared type and the scalar key format; a change resets the state to warming.
- `field_spans(field_key, value_tag, value_key, start_at, start_seq, end_at, row_id)`, `STRICT, WITHOUT ROWID`. Primary key `(field_key, value_tag, value_key, start_at, start_seq, row_id)`, with indexes `(field_key, start_at, start_seq, row_id)` and `(row_id, field_key, start_at, start_seq)`. `value_tag` and `value_key` come from `query_engine.scalars.scalar_key`, so a timeline compares values exactly as a current filter does. `start_at` and `end_at` are UTC microseconds.

How each read holds at 100,000 rows with 5 to 20 versions per row:

- **A value predicate that the key order serves** (`eq`, `in`, ranges, `startswith`) with `at`, or with `from` and `to`: a range scan of the primary key over the matching values with `start_at` up to the window's end, checking `end_at`. The cost is the number of spans that ever held a matching value before the window ends. For a 40-value field that changes in half the versions, about 15,000 entries (estimate).
- **Other predicates, or none, with `at` or a window:** one seek per selected row in the by-row index for the span alive in the window. That is at most 100,000 seeks. This is the path most at risk of the 1 s bound, and the benchmark measures it on its own.
- **`changes`:** a range scan of the by-start index over the window, plus one by-row seek per returned span for the previous value.
- **One row, or a small selection:** the direct path below.

**Direct path.** A selection whose rows hold at most the direct bound of versions, summed from their `row_version`, is answered from the versions and timeline facts. The same span function answers it, validates writes and builds the index. The initial bound is 20,000 versions; the benchmark sets the final value so that a direct answer on a typed collection fits 300 ms.

**Demand and build.** When a selection exceeds the direct bound and the field has no ready index, the query asks the store service for that index and is refused `TIMELINE_WARMING` with `retry_after_ms` and progress. It never returns an empty or partial list. The store service keeps the requests in a bounded in-memory set. The serving store thread's derived pass, which already advances query projections, rollups and the reference index, writes the field's state row and builds it in batches that read versions set-based, one statement per payload layout. Each batch keeps its boundary hold within the import bound, p99 ≤ 0.5 s. A restart loses only an unseeded request, and the next large query asks again.

**Maintenance.** A write to a row that changes an indexed field, or records a past point or correction for it, replaces that row's spans for that field in the same transaction: at most about 20 entries. Retraction removes the row's spans; re-admission rebuilds them. A revision that removes a field drops its index, and one that changes its type resets it.

Storage for one indexed field at the load target is estimated at 70 to 110 MB (unverified). The benchmark records the measured size, and `inspect` lists each built index and its state.

### D8. Governance

- `timeline.field` passes the caller's `FieldPlan` first. A withheld field is refused `QUERY_FIELD_UNKNOWN`, identical to an undeclared field. Only then is a declared but non-scalar field refused `TIMELINE_FIELD_NOT_SCALAR`.
- Spans come only from rows that the caller's current query releases. A withheld row adds no span, count or continuation state. An owner skips the per-row check, as current queries do.
- A span's transition id, `why` and sources go only to a caller who reads the whole row (`reads_whole_row`), because each can carry any field. A caller who reads the field gets its value, its effective times and its write time.
- Writes with `effective_at` or `corrects` keep the existing rule that only a caller who reads every field can write.

### D9. Scope

- **In scope.** Store-backed Records collections in every payload encoding (`json-v1`, `typed-v1`, `typed-v2`) and both view modes.
- **Refused `TIMELINE_UNAVAILABLE`.** File-canonical, dataset and derived collections, after the collection resolves under the caller's authority, as retraction does. A vault with a collection store creates only store collections, so file-canonical collections are the pre-cutover path. Their kept corrections hold prior payloads only, with no version spine to order points.
- **Unchanged.** Import (rows take their write time) and Planning collections, which share the store but take no new argument.
- **Export.** Export to files names a collection that holds a stated timeline fact as unexportable, because file mode cannot hold the fact. Export already keeps only current rows and a checkpoint of store history.

### D10. Delivery order

1. History by natural key and by `item_id` (D6). It needs no schema change, so it can ship on its own.
2. Schema 21, effective time, placements, corrections, timeline queries and the span index, in one batch.

The benchmark cases come first, so the baseline runs on `main` before either delivery.

## Interfaces

**Write arguments**

- `add_record`, `edit_record`: `effective_at` (string, optional): an RFC 3339 instant or a calendar date.
- `edit_record` `options.corrects` (string): a transition id.
- Bulk upsert: a batch `effective_at`, and `effective_at` on each row; a row's own value wins.
- Leaf `record_memory`: `effective_at` on `append`, `update` and `bulk_upsert`; `corrects` on `update`.

**Write replies**

- `append` and `update` receipts gain `timeline: {effective_at, effective_basis, placements, corrects}`. `effective_basis` is `stated` or `written`. `placements` maps each changed field to `current` or `past`. `corrects` is present only on a correction. The stored receipt holds the same block, so a replay returns it.
- A bulk row outcome gains `effective_at` and `effective_basis`.

**History**

- `records.history` takes `natural_key` (object) or `item_id` (string) instead of `item_key`.
- A store-backed revision gains `effective_at`, `effective_basis`, `placements`, `past_values` (field to value, for past points), `corrects`, and `replaced_by` (a list of `{transition_id, written_at, fields}`).

**Query**

- `query_records` takes `timeline: {field, value?, at?, from?, to?, changes?}`.
  - `value` is `{op, value}` with the filter operators; `op` defaults to `eq`.
  - `at` excludes `from`, `to` and `changes`. `changes` needs `from` or `to`.
  - `sort_by`, `aggregate`, `view`, `expand_child`, `expand_children`, `date_from`, `date_to` and the typed `query` are refused with `timeline` (`TIMELINE_ARGUMENT_INVALID`). `descending` gives newest first. `columns` adds those fields' current values to each span under `columns`.
- The reply gains `timeline: {field, window, source}` and `spans`, and keeps the envelope's `total`, `returned`, `truncated`, `continuation` and snapshot.
  - `source` is `index` or `versions`. `window` is `{from, to}` as instants, or null.
  - A span carries `id`, `value`, `previous` (or `first: true` on a row's first span), `effective_from`, `effective_to`, `effective_basis` and `written_at`.
  - A caller who reads the whole row also gets `transition_id`, `why` and `sources`.
  - An absent field value is reported as `missing: true` instead of `value`, and `previous_missing: true` instead of `previous`.

**New refusal codes**

`EFFECTIVE_AT_INVALID`, `EFFECTIVE_BEFORE_CURRENT`, `TIMELINE_ORDER_CONFLICT`, `TIMELINE_POINT_NOT_FOUND`, `TIMELINE_ARGUMENT_INVALID`, `TIMELINE_FIELD_NOT_SCALAR`, `TIMELINE_WARMING` and `TIMELINE_UNAVAILABLE`. `QUERY_FIELD_UNKNOWN` and `RECORD_NOT_FOUND` are reused.

## Risks / Trade-offs

- **Every earlier release refuses a schema-21 store.** Back up the store with the current release before the upgrade. To go back, restore that backup, which loses later writes, or roll forward.
- **The row path may miss 1 s at 100,000 rows.** A query with no key-order predicate costs one seek per selected row. The benchmark measures it alone. If it misses, the delivery stops and the operator chooses between a narrower contract and more index.
- **Each indexed field costs storage and write work.** The estimate is 70 to 110 MB per field at the load target. Only queried fields pay. `inspect` lists them, and the benchmark records the size and the extra cost on edits and imports.
- **The first large query on a field warms.** The agent retries after the hint. Small selections answer at once.
- **Mixed precision can surprise.** A date that ties a timed change on the same day becomes current and orders after it. The reply names the placement and echoes the window, and `describe` teaches the rule.
- **UTC days.** A date names a UTC day, which can differ from a writer's local day near midnight. A vault time zone is a later change.
- **Valid time does not model retraction.** A retracted row is absent from timeline queries. Re-admission restores its whole timeline, including the retracted period.
- **Export blocks collections with stated facts.** This keeps file mode from silently dropping them, at the cost of exportability.
- **A verification bug could fail a sound chain.** Tests alter, add and remove facts and the marker, and reopen cold. The migration rehearsal verifies the chain from the first transaction before and after.

## Migration Plan

1. Run the benchmark baseline on `main`.
2. Deliver history by natural key.
3. Rehearse migration 21 on a byte copy of a production store. Before and after, record the integrity check, `foreign_key_check`, chain verification from the first transaction, `txns` identical in its existing columns, row-identical `item_versions` and `audit_effects`, and per-collection current counts.
4. Back up the store with the running release, then install.
5. On the live service, record a late move with a date on one row, correct it, and read the field's timeline by natural key. Verify the stored facts and the chain.

**Rollback:** restore the pre-upgrade backup, or roll forward.

## Open Questions

- Should file-canonical collections get timelines? This design refuses them, because they have no version spine and the store is the path for new collections.
