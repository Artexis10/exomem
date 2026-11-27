# Tasks

## 1. Markdown table import

- [x] 1.1 Parse a preserved page's tables with the pinned Markdown parser and read one table through the importer's reader; verify an import through preview, start and status that commits the rows citing the Evidence file, and resumes across batches.
      Evidence: `tests/test_collection_store_import_markdown_table.py`.
- [x] 1.2 Type cells: a bad number rejects its row, a bad date is missing, the original text survives in a second field; verify through the same preview and job.
      Evidence: `test_an_agent_previews_and_imports_a_ledger_table_from_preserved_evidence`.
- [x] 1.3 Report per-column cell counts, the rejected cell's text and `check_sum` in preview; verify the counts and per-group sums.
- [x] 1.4 Refuse a non-preserved source with the `save_evidence` route and keep the refusal identical for an absent, an unpreserved and a withheld ref.
      Evidence: `test_a_page_that_is_not_preserved_evidence_is_refused_with_the_save_evidence_route`, and the existing withheld-as-absent test in `tests/test_collection_store_importer.py`.
- [x] 1.5 Reject a row with text beyond the header's width, with its cell count, and report a header fault at its cell; verify through preview, start and status.
      Evidence: `test_a_row_with_text_beyond_the_header_is_rejected_not_silently_cut`, `test_a_header_that_cannot_name_the_columns_is_reported_at_its_cell`.
- [x] 1.6 Parse a page once per distinct bytes, start the batch clock after the parse, bound the parse and refuse a page the reader cannot take whole with advice to import csv; verify through the cache, deadline and limit tests.
      Evidence: `test_a_slow_page_parse_does_not_shrink_the_first_batch`, `test_a_page_too_big_or_too_wide_to_read_is_refused_and_points_to_csv`.
- [x] 1.7 Report per-column counts in job status, including a streamed csv export's, count malformed rows, list the unparsed dates, accept a leading `+`, and make `date` opt-in for csv; verify the counts reconcile with `rows_total`.
      Evidence: `test_an_agent_previews_and_imports_a_ledger_table_from_preserved_evidence`, `test_a_csv_mapping_can_ask_for_a_date_and_then_reports_the_dates_it_could_not_read`, `test_a_streamed_csv_export_reports_its_cells_in_status_as_preview_does`.
- [x] 1.8 Report a null sum, never 0, for a check_sum group with no number, and refuse a scale on a cell that is not a number.
      Evidence: `test_a_check_sum_group_with_no_number_reports_null_and_its_own_missing_count`, `test_a_scale_on_a_cell_that_is_not_a_number_is_refused_when_the_mapping_is_read`.

## 2. Teaching

- [x] 2.1 Serve the `records.import` contract and a worked call through `read_instructions`; verify the taught call previews cleanly.
- [x] 2.2 Add the generic Markdown-table example to the shipped skill and refresh the skill stamps with `scripts/refresh-skill-contract.py`; verify the scaffold no-leak test.
- [x] 2.3 Add the user note.

## 3. Delivery acceptance

- [ ] 3.1 After the batch ships, synchronize the delta into `records` and archive the change with `openspec archive`; verify strict OpenSpec validation.
