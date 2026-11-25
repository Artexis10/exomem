## 1. Benchmark first

- [ ] 1.1 Add timeline cases to the Records benchmark, in `scripts/measure-records-query.py` or a sibling that shares `scripts/_records_measurement_env.py`. The fixture is an invented equipment collection, typed and JSON, at 5,000 and 100,000 rows. Each row has 5 to 20 versions, about 10 on average. A 40-value location field changes in about half the versions. A 4-value status field changes in about a third of them, and one of its values is drawn far more often than the others, so the key-order path meets a low-cardinality, skewed value. Consecutive spans differ, so no value can hold more than about half the spans; the run reports the share the most common value reaches. About 1% of rows hold a past point and 0.5% a correction. The cases are:
  - one row's timeline, selected by a natural-key filter;
  - `at` an instant with `eq`, `at` a date, and a `from`/`to` range with `eq`;
  - `at` an instant with `eq` on the status field's most common value, the costliest key-order case;
  - `at` an instant with `contains`, which takes the per-row path;
  - `changes` over one month and over one year;
  - a current-value filter combined with a timeline;
  - edit by id with `effective_at`, with a past placement and with `corrects`;
  - the 5,000-row import with one indexed field;
  - the index build time for one field, each batch's boundary hold, and the index size in bytes.
  - Check: the script runs the existing cases on `main` unchanged, and the timeline cases on the change branch.
- [ ] 1.2 Record the baseline on `main` for the existing Records bounds (edit by id, filtered query at 5,000 and 100,000 rows, 5,000-row import, boundary hold, import status), 3 repeats each.
  - Check: the run files are kept with the maintainer's evidence, and this task names their dates and results.

## 2. History by natural key (delivery 1, no schema change)

- [x] 2.1 Let `records.history` select a row by exactly one of `item_key`, `item_id` and `natural_key`, on store-backed and file-canonical collections. Resolve `natural_key` to the current row, or else to the retracted row whose `hashed-v1` key derives from it. Apply `FieldPlan.natural_key_withheld` before matching.
  - Check: tests for lookup by key on a typed and a JSON collection; a retracted row found by key; a withheld natural-key field and an absent key giving identical refusals; a stale `item_id` naming the same row.
  - Evidence: `tests/test_record_history_natural_key.py` (9 cases: JSON and typed lookup, retracted row, moved key, withheld field against absent key, unreadable row against absent key, stale `item_id`, selector and key-shape refusals, file-canonical collection). A mutation that removed the `natural_key_withheld` check failed the withheld case.
- [x] 2.2 Update the `history` operation entry, the `inspect_memory` routing and `describe`. Regenerate `docs/capabilities.md` and the MCP tool schema fixture.
  - Check: `scripts/generate-capabilities.py --check` passes, and the regenerated `docs/capabilities.md` adds only the new history argument. The MCP schema fixture stays byte-identical, because `inspect_memory` takes an opaque `arguments` object.
  - Evidence: `scripts/generate-capabilities.py` added `natural_key` to the `record_memory` row (`item_id` was already there). `scripts/dump-tool-schemas.py` left `tests/fixtures/mcp_tool_schemas.json` and the discovery fingerprint unchanged, because `inspect_memory` takes an opaque `arguments` object, so the fixture shows no history arguments at all. The routing is covered by `tests/test_operation_entries.py` and `tests/test_record_history_natural_key.py`.

## 3. Store schema 21

- [x] 3.1 Add migration 21: `version_effective` and `timeline_points` with append-only triggers; `txns.timeline_version` through `ALTER TABLE ... ADD COLUMN`; `field_span_state` and `field_spans` with their indexes, listed in `DERIVED_INDEX_TABLES`. Record the state compatibility id for schema 21.
  - Check: a migration test from schema 20 on a JSON and a typed collection with dynamic tables: `foreign_key_check` is clean, `txns` is identical in its existing columns, `item_versions` and `audit_effects` are row-identical, every row reads, and the chain verifies from the first transaction.
  - Evidence: `tests/test_record_timeline_schema.py::test_migration_keeps_every_row_version_and_proof` migrates a schema-20 store with a JSON and a typed collection, both written by the maintained writer; `tests/test_collection_store_schema.py` covers the four new tables and the append-only triggers. `schema.compatibility_ids` derives the schema-21 id from `SCHEMA_VERSION`.
- [x] 3.2 Add the `timeline` member to the event material when `timeline_version` is 1, and rebuild it in `history.verify` from the two tables.
  - Check: tests in which a changed effective time, an added point, a removed point and a cleared marker each fail verification at their transaction; a cold reopen of a sound store verifies.
  - Evidence: `tests/test_record_timeline_schema.py::test_a_timeline_fact_changed_outside_the_writer_fails_verification_at_its_write` (4 cases, each asserting the failing `commit_seq`) and `::test_a_sound_store_with_timeline_facts_verifies_after_a_cold_reopen`.

## 4. Timeline facts on writes

- [x] 4.1 Write one span function that turns a row's versions and timeline facts into per-field points and spans. The writer, the direct path and the index build all call it.
  - Check: unit tests for each branch: change semantics, the coarser-precision tie, ordering after the followed point, a kept zero-length span, a correction that replaces a point, and the merge of equal values.
  - Evidence: `tests/test_record_timeline_fold.py` (8 tests, 14 cases with the effective-time bounds). The writer and store history call `timeline.fold`; the direct query path and the index build call it when 5.2 and 5.4 land.
- [x] 4.2 Accept `effective_at` on `append`, `update` and `bulk_upsert` (per row and for the batch). Refuse `EFFECTIVE_AT_INVALID`. Add the receipt's `timeline` block and keep it in the stored receipt.
  - Check: tests for a late move, a defaulted write reported `written`, a future date refused, a date one day ahead of UTC accepted, and a request-id replay that returns the same block.
  - Evidence: the six 4.2 tests in `tests/test_record_timeline_writes.py`, from `test_a_move_recorded_late_keeps_its_real_date` to `test_a_bulk_rows_own_effective_time_wins_over_the_batchs`.
- [x] 4.3 Place each field of an `update` as `current` or `past`. Write a new row version for every timeline write. Refuse a past placement in `append` and `bulk_upsert` with `EFFECTIVE_BEFORE_CURRENT`.
  - Check: tests for a missed move, one edit with two placements, the same-day date rule, and a bulk `abort` batch that writes nothing.
  - Evidence: the five 4.3 tests in `tests/test_record_timeline_writes.py`, including `test_bulk_upsert_refuses_a_past_placement_and_an_abort_writes_nothing` and `test_reviving_a_retracted_row_before_its_latest_point_writes_nothing`.
- [x] 4.4 Accept `corrects` on `update`. Refuse `TIMELINE_POINT_NOT_FOUND` and `TIMELINE_ORDER_CONFLICT`.
  - Check: tests for a corrected current value with no change shown between the two values, a re-date with no values, a corrected past value, a correction of a correction, and each refusal writing nothing.
  - Evidence: the 4.4 tests in `tests/test_record_timeline_writes.py`, with `test_a_refused_correction_writes_nothing` covering TIMELINE_POINT_NOT_FOUND twice, TIMELINE_ORDER_CONFLICT and INVALID_RECORD_CHANGES.
- [x] 4.5 Report `effective_at`, `effective_basis`, `placements`, `past_values`, `corrects` and `replaced_by` in store history revisions. Keep an unchanged-payload update that records a fact. Show a stated effective time on the generated history page.
  - Check: a test in which a corrected move shows both revisions, each naming the other.
  - Evidence: `tests/test_record_timeline_writes.py::test_history_shows_a_corrected_move_and_its_correction_each_naming_the_other`, which also checks a revision that changed only a past point and the page's effective times.
- [x] 4.6 Refuse `effective_at`, `corrects` and `timeline` with `TIMELINE_UNAVAILABLE` on file-canonical, dataset and derived collections. Make the export preview name each collection with timeline facts and how many the export drops.
  - Check: tests for each refused kind writing nothing, and for an export preview that names the dropped count of a collection with a past point.
  - Evidence: `tests/test_record_timeline_writes.py::test_a_file_collection_refuses_a_timeline_argument_and_writes_nothing` (file-canonical and dataset, add and edit), `::test_a_derived_collection_refuses_a_timeline_argument_and_writes_nothing` and `tests/test_collection_store_export.py::test_an_export_preview_names_the_timeline_facts_it_drops`. The `timeline` query argument arrives with 5.1, which adds its refusal.

## 5. Timeline queries

- [x] 5.1 Add `timeline` to `query_records` in `public_tools`, `record_memory` and `operation_entries`, with `TIMELINE_ARGUMENT_INVALID` and `TIMELINE_FIELD_NOT_SCALAR`.
  - Check: the MCP tool schema fixture shows the one added argument; tests for each refused combination.
  - Evidence: `tests/test_record_timeline_query.py::test_a_timeline_query_refuses_an_argument_it_does_not_honour_and_names_it`, `::test_a_malformed_timeline_is_refused_naming_its_part`, `::test_a_field_that_is_not_a_top_level_scalar_is_refused` and `::test_an_undeclared_field_is_refused_with_the_fields_it_can_name`, on a JSON and a typed collection. `scripts/dump-tool-schemas.py` added only `query_records.timeline` to `tests/fixtures/mcp_tool_schemas.json` (`tests/test_mcp_schema_fidelity.py`); it adds 124 B, which `tests/test_tool_schema_budget.py` records. The 4.6 refusal tests in `tests/test_record_timeline_writes.py` now also send `timeline`.
- [x] 5.2 Answer selections within the direct bound from the versions: one row's timeline, `at` an instant, `at` a date, a range, `changes` with `previous` and `first`, `descending`, `columns`, and continuations bound to the snapshot.
  - Check: tests for each case on a typed and a JSON collection, including a retracted row's absence and a continuation refused after a write.
  - Evidence: `tests/test_record_timeline_query.py::test_one_rows_timeline_lists_every_span_with_its_times`, `::test_an_instant_returns_the_spans_that_contain_it`, `::test_a_date_returns_every_span_that_overlaps_its_utc_day`, `::test_a_range_returns_the_spans_that_overlap_it_through_its_last_day`, `::test_changes_list_the_spans_that_started_in_the_window_with_the_value_before`, `::test_descending_lists_the_newest_span_first`, `::test_columns_add_each_rows_current_values`, `::test_a_continuation_resumes_the_same_spans_and_is_refused_after_a_write` and `::test_two_moves_stated_on_one_date_both_stay_and_keep_their_order`, each on a JSON and a typed collection. `::test_a_retracted_row_has_no_spans` runs on the typed collection only: a JSON collection keeps one view file per row and refuses a retraction with `RETRACT_UNAVAILABLE`.
- [ ] 5.3 Apply governance: `QUERY_FIELD_UNKNOWN` for a withheld field, no trace of a withheld row, and no transition id, `why` or sources for a field reader.
  - Check: tests with a field-limited audience and a row-withheld audience, comparing each refusal and total with its undeclared or absent twin.
- [ ] 5.4 Build the span index on demand: the request set, the state row, batched builds within the hold bound, maintenance on writes, retraction and re-admission, a reset on a field's removal or type change, and empty copies in snapshots and replicas.
  - Check: tests in which the index and the direct path return identical spans, totals and order for every case of 5.2; a large selection before the build refused `TIMELINE_WARMING` with a retry hint and progress, never an empty list; a restored snapshot that warms.
- [ ] 5.5 List each built timeline index and its state in `inspect`.
  - Check: a test of an inspect reply before, during and after a build.

## 6. Contract and guidance

- [ ] 6.1 Teach `effective_at`, its basis, placements, `corrects`, `timeline` and the new codes in `describe`, with the lamp example.
  - Check: a test client that reads only `describe` records a late move, corrects it and queries the timeline; `tests/test_scaffold_no_leak.py` passes.
- [ ] 6.2 Update the scaffold skill reference (`references/operations.md`), `docs/records.md` and the schema-21 note in `docs/deployment.md`, and regenerate `docs/capabilities.md`.
  - Check: the no-leak test and the public-artifact privacy gate pass.

## 7. Proof and closure

- [ ] 7.1 Run the Records benchmark on the change branch with the cases of 1.1 and the bounds of 1.2, 3 repeats each.
  - Check: each timeline bound holds at 5,000 and 100,000 rows, and each existing bound holds, as a per-case difference against the baseline with its 95% interval. The run files are kept, and this task records the results, the index size and the build time. If a bound fails, stop and report to the operator before release.
- [ ] 7.2 Add one step to the installed-artifact Records release journey: record a late move, correct it and read the field's timeline by natural key through a real MCP transport.
  - Check: the release gate passes with the new step.
- [ ] 7.3 Rehearse migration 21 on a byte copy of a production store.
  - Check: before and after, the integrity check, `foreign_key_check`, chain verification from the first transaction, `txns` identical in its existing columns, row-identical `item_versions` and `audit_effects`, per-collection current counts and the migration time are recorded here.
- [ ] 7.4 After the install, with a backup taken before the upgrade, record a late move with a date on one row of a live store collection, correct it, and read its timeline by natural key.
  - Check: the stored `version_effective` and `timeline_points` rows match the replies, and the collection's history verifies.
- [ ] 7.5 Archive this change with `openspec archive record-value-timeline` after 7.4.
  - Check: `openspec validate --all --strict` passes before and after the archive.
