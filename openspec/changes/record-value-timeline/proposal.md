## Why

A store-backed Records row keeps every version of its values, but the product reads none of that history as a timeline. A person who asks "where was the lamp in March?" or "what stood on Shelf A on 1 March?" gets no answer. Four gaps cause this:

- A version's only time is its write time. A move that someone records a week late reads as a move on the day it was written.
- Row history (`records.history`) needs the row's internal UUID. The key a person knows, such as an asset tag, is refused `RECORD_NOT_FOUND`.
- No query reads values over time. The only history surface returns one whole row's revisions, and only to a caller who reads every field.
- A wrong date or a wrong value has no correction that keeps what was believed before.

These gaps apply to every collection and every field. A location, a status and a borrower are ordinary fields, so the fix is generic: no code knows a field name, and no collection needs a setting.

## What Changes

- **Effective time on writes.** `add_record`, `edit_record` and bulk upsert take an optional `effective_at`: an RFC 3339 instant or a calendar date when the written values became true. A write that states none takes its write time. Every reply, history revision and timeline span marks the basis as `stated` or `written`.
- **Field timelines.** Each field of a row has a timeline of points. A write adds a point to a field only where it changes the field's value at the write's effective time. The latest point of each field always holds the row's current value.
- **Past values.** An `edit_record` whose effective time is earlier than a field's latest point records a past point for that field and keeps the current value. The reply names each field's placement, `current` or `past`. `add_record` and bulk upsert change current state only and refuse a past placement.
- **Corrections.** `edit_record` takes `corrects`, the transition id of the write that recorded a point. The new point replaces that point in the timeline. History keeps both with their write times, so what was believed at each time stays readable.
- **History by natural key.** `records.history` selects a row by `natural_key` or by the row `id` that a query returns, as well as by `item_key`. Each revision reports its effective time, basis, placements and corrections.
- **Timeline queries.** `query_records` takes `timeline: {field, value, at | from, to, changes}` and returns spans of one field: the value, when it took effect, when it stopped and when it was written. `filters` still select rows by their current values. This one argument answers three questions: one row's values over time, the rows that held a value at a time or during a range, and the rows that changed the field in a range.
- **Governance.** A withheld field's timeline is refused exactly as an undeclared field's is. A withheld row contributes nothing. `why`, sources and transition ids go only to a caller who reads the whole row.
- **Scale.** The first large query on a field starts a per-field span index, and writes keep a built index current. Small selections are answered directly from the versions. Until an index is built, a large query answers `TIMELINE_WARMING` with a retry hint, never a partial list.
- **Store schema 21** adds two append-only timeline tables, the span index and a transaction marker that brings the timeline facts into the hashed history chain.
- File-canonical, dataset and derived collections refuse the new arguments with `TIMELINE_UNAVAILABLE`. Import is unchanged: its rows take their write time.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `records`: adds effective time, field timelines, past values, corrections, history by natural key and timeline queries.

## Impact

- Store schema 21. Every earlier release refuses a schema-21 store. To go back after the upgrade, restore the backup taken before it, or roll forward.
- Public tools: `add_record` and `edit_record` gain `effective_at`; `edit_record` gains the `corrects` option; `query_records` gains `timeline`; `records.history` gains `natural_key` and `item_id`. No tool is added.
- `collection_store` schema, writer, history, snapshot and export; a new timeline module; `record_memory`, `record_history`, `operation_entries`, `public_tools`, `describe`, the scaffold skill reference and the generated capability docs.
- The Records benchmark gains timeline cases at 5,000 and 100,000 rows.
