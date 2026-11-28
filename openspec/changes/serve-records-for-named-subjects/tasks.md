## 1. Declaration

- [x] 1.1 Parse `subject` and the field `description` in Records manifests; refuse a malformed declaration in parsing, revise and validate with an error that names what to change; publish both in the authoring contract.
  - Evidence: `parse_subject`, `_subject_field`, `SubjectDeclaration`, `FieldSpec.description` and `_subject_json_schema` in `src/exomem/structured_collections.py`. Tests in `tests/test_subject_records.py`: `test_a_subject_declaration_parses_with_its_defaults`, `test_a_malformed_subject_is_refused_naming_what_to_change` (11 cases), `test_only_a_records_collection_declares_a_subject`, `test_creating_validating_or_revising_a_ledger_refuses_a_malformed_subject`, `test_a_manifest_that_used_subject_for_something_else_stays_readable`.

## 2. The block

- [x] 2.1 Read the sums of a cited page under the citation reader's admission, after one visibility probe and in two statements per pair, and attach them to the served packet inside its budget.
  - Evidence: `src/exomem/subject_records.py`; `page_citations.citing_rows`; `NativeLayout.cell_sql`; the hook in `commands.op_activate_context` before `_cite_packet_leads`, which has the citation read count, but not list, the citations that a lead's `ready` entry stands for (`page_citations.for_hits`, `unlisted`). Tests in `tests/test_subject_records.py`:
    - contents and unknowns, typed and JSON rows: `test_a_turn_naming_a_person_and_the_ledger_gets_their_sums_unknowns_and_newest_rows`;
    - the gate: `test_naming_a_person_without_touching_the_ledger_serves_no_sums`, `test_touching_the_ledger_without_naming_a_person_its_rows_cite_serves_no_sums`, `test_a_lead_the_turn_does_not_name_gets_no_sums`;
    - a starved task context: `test_a_request_whose_task_context_ran_out_of_time_still_serves_the_sums`;
    - statements: `test_the_sums_read_the_store_twice_however_many_rows_cite_the_person`, `test_a_collection_that_declares_no_subject_costs_the_block_nothing`;
    - the cap and its stub: `test_a_turn_naming_four_people_the_ledger_cites_gets_the_first_three`;
    - visibility, proof first: `test_a_caller_who_may_not_read_the_amount_gets_no_sums_and_is_told_why`, `test_a_caller_who_may_read_neither_the_amount_nor_any_of_the_rows_is_told_nothing`, `test_a_row_the_caller_may_not_read_leaves_no_trace_in_the_sums`;
    - warming: `test_an_index_still_building_says_warming_instead_of_a_zero_total`;
    - the block's own state: `test_a_freshly_declared_ledger_says_warming_without_naming_anyone`, `test_a_page_index_that_cannot_be_read_makes_the_block_unavailable_for_any_page`, `test_a_deadline_spent_proving_a_pair_makes_the_block_unavailable`, `test_a_deadline_spent_admitting_a_ledger_makes_the_block_unavailable`;
    - a collection's own refusal: `test_a_collection_that_cannot_be_admitted_leaves_the_other_ledgers_served` (both id orders);
    - an entry's own failure: `test_a_sum_that_cannot_be_read_makes_its_own_entry_unavailable`;
    - budget, stubs and the lead's citations: `test_a_ledger_whose_newest_rows_do_not_fit_keeps_its_sums`, `test_a_nearly_full_packet_names_the_ledger_it_had_no_room_for`, `test_a_ledger_entry_keeps_the_leads_citations_from_other_collections` (both id orders), `test_a_ledger_entry_without_newest_rows_leaves_the_leads_citations_whole`, and the `cited_by` and `summary` assertions in the first test; in `tests/test_page_citations.py`, `test_an_unlisted_field_is_counted_and_leaves_the_hit_s_citations_to_the_other_fields`;
    - `show` refuses a link field: the `show-a-link` case of `test_a_malformed_subject_is_refused_naming_what_to_change`;
    - decimal sums, a sum that cancels to zero, datetime order and a long description: `test_decimal_amounts_sum_to_the_places_they_were_written_with` (float noise and cancelling to zero, both layouts), `test_the_newest_rows_by_datetime_follow_the_instant_not_the_text`, `test_a_long_measure_description_is_cut_so_the_sums_still_fit`;
    - hook delivery, rendered by the shipped hook from blocks the server served: `test_a_hook_served_turn_sees_the_tally_and_what_its_sign_means` (map and rollback layouts), `test_a_hook_served_turn_hears_that_a_ledger_is_still_warming`, `test_a_hook_ceiling_too_small_for_any_tally_line_still_says_the_tallies_exist`.
- [x] 2.2 Measure the block against the Records load target per pair.
  - Evidence: `benchmarks/subject_records/latency.py` and its recorded run in `benchmarks/subject_records/results.json`, summarised in `benchmarks/subject_records/README.md`.
- [x] 2.3 Teach the declaration in the skill scaffold and add a user note.
  - Evidence: `src/exomem/_scaffold/_Schema/references/planning-records.md`; `user-notes/running-tallies-in-recall.md`; `tests/test_scaffold_no_leak.py` passes.

## 3. Delivery

- [ ] 3.1 One blind independent review of the batch PR.
- [ ] 3.2 Real-model round (`eval-loop`, subscription models only): a turn that asks what a named person owes, and one that names the person without the tally. Record whether the agent answers from `subject_records`, reports an unknown amount as unknown, and stays silent on the tally when the turn does not ask.
- [ ] 3.3 Operator vault step after release:
  1. Declare `subject` on the target Records collection.
  2. Check that its storage is a Markdown-items collection or a typed-v2 summary collection, since any other encoding reads `unavailable`.
  3. Check that the incident turns hold at least one term of the collection's effective `claims`.
  4. If a turn holds none, add its term to the manifest's `claims`.
  5. Run the real turn once, and check that the person's lead in `turn_slice.items` carries `exact_alias` in its `evidence`.
  6. If it does not, add the name that the turn uses to `aliases` on the person's entity page, and run the turn again.
- [ ] 3.4 Sync the deltas into the canonical specs and archive this change with `openspec archive`, after the strict gate passes.
