# Tasks — repair the Records writer

Every test lands red first (failing output recorded before the implementation, then green). Scoped suites are named in each task; the full suite runs once at the end. Measured sizes and pins are written into this file when the task is ticked, not estimated. On a machine running the live personal service, every pytest invocation sets `XDG_STATE_HOME` to a scratch directory so the conftest state-root guard watches an inert root (design, Risks); the baseline before any change was 365 passed, 0 failed, 135 guard errors from that interference.

## 1. Validation — field-addressed refusals and strategy-aware representability (design D1, D2)

- [x] 1.1 `records._validate_values`: collect issues instead of raising on the first — each `{field, code, reason, received}` with dotted/indexed paths (`metrics[0].source`); `SCHEMA_UNKNOWN_FIELD` lists every undeclared field; schema type and enum failures are aggregated (wrap `manifest.schema.validate` field by field if it only raises). The refusal keeps the first issue's code and message and carries `details.issues` plus `details.field`. Red-first tests in `tests/test_record_mutation.py`: two invalid values plus one undeclared field → one refusal with three issues; nested path addressed; existing single-failure tests still pass on code and message.
  Landed: issues carry `{field, code, reason, received}`; the refusal keeps the
  first issue's code and the shipped message (`SCHEMA_UNKNOWN_FIELD` still reads
  "item uses fields outside the schema") and adds `details.field` plus
  `details.issues`. Array elements are addressed by index and object sub-fields by
  dotted path. Tests: `test_item_refusal_names_every_failing_field_in_one_response`
  (3 issues), `test_every_undeclared_field_is_named`,
  `test_array_element_failure_is_addressed_by_index` (`movements[2]`),
  `test_nested_object_sub_field_failure_is_addressed_by_path` (`metrics[2].value`).
- [x] 1.2 Strategy-aware representability: the check receives the storage strategy and the field path; `markdown-log` keeps refusing `\n`/`\r` (and the byte cap) naming the field; `markdown-items` keeps only the byte cap. Red-first tests: markdown-log heading/note/child-row newline refuses naming the field; markdown-items newline passes this stage.
  Landed: `records._collect_representation_issues` allows line breaks only for
  `markdown-items`; the byte cap applies to every strategy. Tests:
  `test_markdown_log_still_refuses_a_line_break_and_names_the_nested_field`
  (`movements[1].movement`), `test_markdown_log_refuses_a_line_break_in_a_heading_field`.
- [x] 1.3 Round-trip proof for `markdown-items`: serialise the complete candidate frontmatter with `vault.serialize_frontmatter`, parse back with the adapter's frontmatter reader, normalise both sides through the schema, compare field by field; any difference refuses `UNREPRESENTABLE_RECORD_VALUE` naming the field and stages nothing. Red-first tests: a string with two consecutive line breaks commits and reads back equal, and `sha256` of the read-back value equals `sha256` of the candidate; date, datetime, integer, nested object and array-of-object fields round-trip; a monkeypatched serializer that corrupts one field yields a refusal naming that field with no staged or partial file.
  Landed: `records._collect_round_trip_issues` serialises the complete candidate
  frontmatter with `vault.serialize_frontmatter`, parses it with
  `vault.parse_frontmatter(text, strict=True)` (the reader `MarkdownItemsAdapter.read`
  uses) and compares field by field after
  `structured_collections.normalize_item_values`, which normalises date and
  datetime fields on both sides. Tests:
  `test_markdown_item_commits_multi_line_text_and_reads_it_back_identically`
  (sha256 of the read-back value equals sha256 of the candidate),
  `test_typed_markdown_item_fields_survive_the_round_trip` (date, datetime,
  integer, object, array-of-object, array-of-string),
  `test_round_trip_refuses_when_serialisation_would_lose_a_field` (monkeypatched
  serializer; refusal names `exact_text` and the item directory stays empty).
  No serializer defect was found: every typed field round-tripped unchanged.
- [x] 1.4 Planning parity: a `plan_memory(action="add")` with a multi-line string field on a Markdown-item Planning collection commits through the shared validator with no Planning argument changes. Red-first test in the Planning suite; `tests/test_plan*.py` green.
  Landed with no Planning argument change: the rule lives in the shared
  validator. Test `test_plan_memory_add_commits_a_multi_line_string_field`
  (`tests/test_plan_memory_command.py`). Planning suites: 203 passed, 0 failed.

## 2. Identity remediation (design D3)

- [x] 2.1 `append_record` / `update_record`: a non-UUID `item_key` that equals a natural-key field value of the candidate, or arrives while the candidate's natural key is complete, refuses `INVALID_RECORD_ID` with `details.natural_key`, `details.received` and the remediation sentence; other non-UUID keys keep the current message plus `details.received`. `describe` gains the identity sentence; `record_memory` docstring says `item_key` is the internal UUID. Red-first tests in `tests/test_record_mutation.py` and the describe/authoring test.
  Landed: the message stays "record ID must be a UUID"; `details` always carries
  `argument` and `received`, and adds `natural_key` plus `remediation` when the
  supplied value equals a natural-key field value or the candidate's natural key
  is complete. `update_record` resolves the collection only after the key is
  already known to be non-UUID, and only through the mutation resolver, so a
  withheld collection is not disclosed by the remediation path. `describe` gains
  `item_identity`; the `record_memory` and `op_record_memory` docstrings name
  `item_key` as the internal UUID. Tests:
  `test_natural_key_value_supplied_as_item_key_refuses_with_remediation`,
  `test_complete_natural_key_with_any_non_uuid_item_key_gets_remediation`,
  `test_other_non_uuid_item_keys_keep_the_plain_refusal`,
  `test_update_with_a_natural_key_value_as_item_key_gets_remediation`,
  `test_describe_states_that_item_key_is_the_internal_uuid`,
  `test_record_memory_docstring_names_item_key_as_the_internal_uuid`.

## 3. Held records (design D4)

- [x] 3.1 Held file writer (`records.hold_candidate`): `<collection dir>/Held/<held_id>.md` with generated single-line frontmatter (`type: held-record`, `collection_id`, `held_id`, `attempted_action`, `target_item_key`, `held_at`, `why`, `candidate_sha256`, `diagnostics`) and one fenced `json` body carrying the exact candidate (`item` or `changes`/`delete_fields`, `body`, guards); `held_id` is the derived item key when the natural key is complete, else a fresh UUID; re-holding the same candidate rewrites the same file. Red-first tests: file shape, JSON body equals candidate byte-for-byte after load, same-candidate re-hold yields one file, and a manifest whose `storage.source` is `.` (so `Held/` would fall under the source) refuses the hold and returns the original refusal plus a warning with no file written.
  Landed as `records.hold_candidate`. Deviation recorded: `diagnostics` is a
  single-line JSON *string* in the frontmatter, because `serialize_frontmatter`
  renders a YAML list of objects as a multi-line block and the design's binding
  constraint is that every generated frontmatter value is single-line. The
  structured list reads back exactly via `json.loads`, and the test asserts that.
  Tests: `test_a_refused_append_holds_the_complete_candidate` (file shape, every
  frontmatter value single-line, JSON body equals the candidate after load),
  `test_re_holding_the_same_candidate_rewrites_one_file`,
  `test_hold_refuses_when_the_held_directory_would_fall_under_the_item_source`
  (`storage.source: .`; original refusal plus a warning, no file, no directory).
- [x] 3.2 Wire holding into append/update refusals for candidate-content codes (`UNREPRESENTABLE_RECORD_VALUE`, `SCHEMA_UNKNOWN_FIELD`, schema type/enum codes); the refusal response carries `held: {held_id, path, diagnostics}`; `hold=false` skips the file; a monkeypatched hold failure returns the original refusal unchanged plus a warning and no `held`. Red-first tests for each branch; guard-refusals (stale hash, natural-key conflict) never hold.
  Landed: `_CANDIDATE_CONTENT_CODES` is exactly `UNREPRESENTABLE_RECORD_VALUE`,
  `SCHEMA_UNKNOWN_FIELD`, `SCHEMA_FIELD_TYPE`, `SCHEMA_ENUM` — the four the spec
  enumerates. `SCHEMA_REQUIRED_FIELD` is aggregated into `details.issues` but does
  NOT hold, because the spec's parenthetical ("representability, undeclared field,
  schema type or enum failure") reads as exhaustive; flagged for review.
  The pre-lease `_validate_values` swallows exactly these codes when holding is on
  so the guarded pass raises them from inside the mutation boundary; every other
  code still fails fast before the writer lease. Tests:
  `test_a_refused_append_holds_the_complete_candidate`,
  `test_declining_the_hold_refuses_plainly`,
  `test_a_hold_failure_does_not_mask_the_refusal` (monkeypatched
  `records.hold_candidate`), `test_guard_refusals_never_hold` (STALE_RECORD),
  `test_natural_key_conflict_never_holds`,
  `test_a_refused_update_holds_the_changes_and_names_the_target`.
- [x] 3.3 Resume: `append`/`update` with `held=<held_id>` loads the file, verifies it lives under this collection and carries its `collection_id` (else `HELD_NOT_FOUND` / `HELD_COLLECTION_MISMATCH` naming the argument), applies shallow overrides from `item`/`changes` (`null` removes), runs the ordinary guarded path; success commits exactly one item and removes the held file inside the same mutation boundary; removal failure after commit warns `HELD_CLEANUP_FAILED`; a repeated refusal rewrites the held file in place with the same `held_id`. Red-first tests: commit once and audit head advances exactly once; held file gone; repeated refusal same id and one file; mismatch refusals.
  Landed: the held file is loaded inside the guard, overrides are shallow with
  `null` removing a field, and removal runs after the item's batch write inside
  the same `mutation_guard`. Deviation recorded: removal is a separate step after
  the commit rather than part of the item's atomic batch, which is what the design
  itself anticipates by specifying `HELD_CLEANUP_FAILED` for "removal fails after
  the item is committed". Resume does not adopt the held candidate's stored guards;
  the caller supplies current ones, so a stale guard from hold time cannot refuse a
  good resume. Tests:
  `test_resuming_a_held_candidate_commits_once_and_removes_the_file` (audit head
  advances, one item, held file gone),
  `test_a_resumed_candidate_that_refuses_again_reuses_one_held_file` (same id, one
  file, diagnostics rewritten),
  `test_resuming_an_unknown_reference_names_the_argument` (HELD_NOT_FOUND),
  `test_resuming_a_reference_from_another_collection_refuses`
  (HELD_COLLECTION_MISMATCH),
  `test_a_failed_cleanup_after_commit_warns_and_keeps_the_item`
  (HELD_CLEANUP_FAILED warning, item stands).
- [x] 3.4 `discard` action in `record_memory` (`collection`, `held`, `why` required): removes the held file, audit chain untouched, response reports the removed reference. Red-first tests including missing/mismatched reference.
  Landed as `records.discard_held`, dispatched from `record_memory(action="discard")`.
  A new selector adapter `record_memory.action=discard -> mutation` was required by
  the shipped egress selector-coverage gate. Tests:
  `test_discarding_a_held_candidate_removes_it_without_touching_the_audit_chain`,
  `test_discarding_an_unknown_reference_names_the_argument`,
  `test_discard_requires_its_complete_set`.
- [x] 3.5 Invisibility pins: with a held file present, adapter item count, `query` results, audit head, source census and recall candidates (`recall_policy` boundary plus a vault `find`) are unchanged. Mutation probe: the same test placed under `Items/` must fail, proving the pins bite. Red-first tests.
  Landed with no exclusion code: `Held/` is a sibling of `storage.source`, which
  `MarkdownItemsAdapter.read` alone walks. Test
  `test_a_held_file_is_invisible_to_items_query_audit_census_and_recall` pins item
  count, container snapshot, source-version paths, audit head, the `query` payload
  (minus its wall-clock `generated_at`), `recall_policy.is_recall_candidate` and the
  `walk_vault_md` + `iter_recall_markdown` candidate set. Two probes prove the pins
  bite: `test_the_source_census_pin_bites_when_the_same_file_sits_under_the_item_source`
  (same bytes under `Entries/` moves the snapshot and adds a source version) and
  `test_the_recall_pin_bites_for_the_same_bytes_outside_the_records_layer` (same
  bytes under `Knowledge Base/Notes/` ARE a recall candidate).
- [x] 3.6 Disclosure: held candidates pass the per-item release filter before being counted or listed; a withheld candidate contributes neither reference nor count. Red-first test reusing the existing withheld-item fixture pattern from `tests/test_record_governance.py`.
  Landed: `records.held_candidates(..., authorize_path=...)` applies the caller's
  per-item release filter before a candidate is read, counted or named, and both
  coverage surfaces pass the governance filter in. Test
  `test_a_withheld_held_candidate_contributes_neither_reference_nor_count`
  (`ceiling: 0` on `Records/Publications/Held/**` for the external audience;
  inspection reports `held: 0` with no references and inventory reports `held: 0`).

## 4. Coverage (design D5)

- [x] 4.1 `record_governance.inspect_collection` gains `coverage: {committed, held, held_refs}` (`held_refs` bounded to 20, each `held_id`, `held_at`, `attempted_action`, one-line diagnostics summary, never values); `inventory_collections` gains per-collection `committed` and `held`. Red-first tests in `tests/test_record_governance.py`: counts, bound, governance filter on both surfaces, egress shape checks pass.
  Landed: `coverage` needed a new entry in `_INSPECTION_KEYS`, the
  `record_inspection` projector tuple, and a `_inspection_coverage` validator, so
  the block is reconstructed field by field before egress rather than passed
  through. `held_refs` carries only `held_id`, `held_at`, `attempted_action` and a
  one-line `CODE: field` diagnostics summary — never a candidate value.
  Deviation recorded: inventory's `committed` is `None`, not `0`, when a
  collection's items cannot be read, because zero would claim a count the sweep
  does not have. Tests: `test_inspect_reports_coverage_for_committed_and_held`
  (also asserts no candidate value appears anywhere in the block),
  `test_inspect_bounds_held_references` (21 held, 20 references),
  `test_inventory_reports_committed_and_held_per_collection`,
  `test_a_withheld_held_candidate_contributes_neither_reference_nor_count`.

## 5. Surface and contract (design D6)

- [x] 5.1 `record_memory` argument matrix: `held` and `hold` on `append`/`update`; new `discard` action with its required set; `_validate_arguments` refuses `held`/`hold`/`discard` fields on other actions naming the argument; MCP schema fixtures and `tests/test_record_memory_command.py` updated; tool-surface fingerprint re-pinned deliberately by regenerating `src/exomem/tool_surface_contract.json` and `tests/fixtures/mcp_tool_schemas.json` with `scripts/dump-tool-schemas.py` (`tests/test_tool_surface_fingerprint.py`, `tests/test_mcp_schema_fidelity.py`, `tests/test_tool_annotations.py` green). Record the old (`ebc02dd769a14cc9e962fdfaf53138ed2bad0a3d74f9c54ea940e529513d26c7`, 29 tools) and new fingerprint here.
  **Tool-surface fingerprint re-pinned deliberately.**
  Old: `ebc02dd769a14cc9e962fdfaf53138ed2bad0a3d74f9c54ea940e529513d26c7`, 29 tools.
  New: `d1e78d2efae5a0129adc90226af53c80750ca35926eeef5184145c8c038b9eae`, 29 tools.
  Reason: `record_memory` gains the `discard` action and the `held` and `hold`
  arguments, and its `action`/`item_key` descriptions change. No tool was added or
  removed. Regenerated with `PYTHONPATH=src uv run python scripts/dump-tool-schemas.py`,
  which rewrote `src/exomem/tool_surface_contract.json` and
  `tests/fixtures/mcp_tool_schemas.json` together. The ChatGPT connector
  attestation (`deploy/chatgpt/personal-plugin-contract.json`) is deliberately NOT
  updated, as that script's own contract requires.
  Two shipped surfaces had to move with it, and both were found by their own gates
  rather than guessed: the MCP tool is built from `commands.op_record_memory`, not
  from `record_memory.record_memory`, so its signature and docstring carry the new
  arguments; and `governance.egress._SELECTOR_ADAPTERS` needed
  `record_memory.action=discard -> mutation` or the shipped selector-coverage gate
  refuses the action before invocation.
  `hosted-alpha-agent-v2` is unchanged and stays at nine actions with no `held`
  or `hold`: it is a frozen historical Hosted profile whose published contract must
  not follow the live registry (`hosted_legacy_schemas`). `tests/test_record_public_surface.py`
  now pins that frozen list separately and asserts the two arguments are absent
  from it. Suites green: `test_tool_surface_fingerprint.py`,
  `test_mcp_schema_fidelity.py`, `test_tool_annotations.py` (17 passed),
  `test_hosted_legacy_profile_pin.py`.
- [x] 5.2 Scaffold references `src/exomem/_scaffold/_Schema/references/planning-records.md` and `references/mutation-results.md`: one sentence each (a refused Record write is held, the response names the field, fix and resume by `held` reference; do not preserve diagnostic breadcrumbs as Evidence). Plugin copy md5-identical; hosted skill renders regenerated with the existing scripts; `tests/test_scaffold_no_leak.py` and the skill-sync/parity tests green. Bootstrap text untouched: assert the compact bootstrap byte size is unchanged and record it here.
  **Compact bootstrap byte size is unchanged: 59,737 bytes before and after**
  (full: 113,279 before and after), measured with `commands.op_bootstrap` on an
  empty fixture vault exactly as `tests/test_bootstrap_compact_budget.py` measures
  it. `commands.op_bootstrap` text was not edited.
  Both reference copies are md5-identical:
  `planning-records.md` `8c8a8ace8600b4dd0ffdc89f0d9c1c91`,
  `mutation-results.md` `f69f82323efc5c58dca4c732ed4cc0ce`.
  Editing the references moves the packaged skill-contract digest, so
  `scripts/refresh-skill-contract.py` re-stamped every canonical `SKILL.md`
  (`c29752b4969560fe1781f0487c2bb39bdcb300cabc8ff9f145d91da49a7bebe2` ->
  `05940623190a01586f864e50c3c21bb8562995f7b61a6b7cc4367c91e355729e`); the
  plugin `SKILL.md` copy was synchronised by hand because the script stamps only
  the scaffold, and the two were equal before. Green:
  `tests/test_workflow_skills.py`, `tests/test_scaffold_no_leak.py`,
  `tests/test_bootstrap_compact_budget.py`, `tests/test_bootstrap.py`. No hosted
  render is committed for these two references, so nothing else needed regenerating.
- [x] 5.3 `describe` documents `held`, `hold`, `discard`, the held file shape, and the `item_key` versus natural-key sentence; the describe contract test pins it.
  Landed as the `held_records` and `item_identity` blocks of
  `structured_collections.manifest_authoring_contract()`. Tests:
  `test_describe_documents_held_records`,
  `test_describe_states_that_item_key_is_the_internal_uuid`.

## 7. Review corrections (independent review round 1, REQUEST_CHANGES)

- [x] 7.1 BLOCKER — holding is Records-only: `_hold_refusal` returns the plain refusal for any manifest whose `semantic_profile` is not `records`; red-first Planning test pins that a refused `plan_memory(action="add")` writes no `Held/` directory and that `plan_memory(action="inspect")` reports no coverage; `tests/test_plan*.py` green.
  Landed as `records._holding_enabled(manifest, hold)`, consulted both by
  `_hold_refusal` and by the pre-lease `_validate_values` swallow (`records.py`
  ~L239), so a non-Records refusal also fails fast before the writer lease again
  rather than being deferred into the guarded pass for a hold that will not
  happen. Test `test_a_refused_planning_add_writes_no_held_file`
  (`tests/test_plan_memory_command.py`); red before the fix on
  `assert not (... / "Knowledge Base/Planning/Work/Held").exists()`.
  `tests/test_plan_memory_command.py`: 23 passed.
- [x] 7.2 MAJOR — markdown-log grammar tokens (row delimiter, heading separator, note bracket) are collected as representability issues with field paths in the validation pass and hold like line breaks; red-first tests for each token (delimiter in a child row, separator in a heading value, bracket in the note) asserting `details.field`, `details.issues` and a held file; the render-layer checks remain and are pinned as unreachable-first.
  Landed as `record_formats.log_grammar_tokens(manifest)` (a public accessor that
  reuses the shipped `_heading_grammar` and `_child_grammar` parsers and returns
  `None` when the descriptor does not parse, because an unparseable descriptor is
  a manifest fault and not a candidate fault) plus
  `records._collect_log_grammar_issues`. Deviation recorded: the heading-separator
  check applies only to heading fields declared `type: string`. A dated heading
  field renders through a manifest-owned `strftime` format, so its raw value is
  not what reaches the heading, and checking it would refuse an ISO date whenever
  a manifest chose `-` as its separator. Those stay with the render layer.
  Red before the fix: all three refused `UNREPRESENTABLE_RECORD_VALUE` with
  `details == {}` (`KeyError: 'field'`) and no held file.
  Tests: `test_a_child_row_carrying_the_row_delimiter_is_addressed_and_held`
  (`movements[1].movement`), `test_a_heading_value_carrying_the_heading_separator_is_addressed_and_held`
  (`title`), `test_a_note_carrying_its_own_bracket_is_addressed_and_held` (`note`),
  and `test_the_render_grammar_checks_remain_as_the_last_line_of_defence`, which
  calls `render_markdown_log_item` directly and pins that all three render
  refusals still fire with their shipped reasons.
- [x] 7.3 MAJOR — `discard` receipt: extend `mutation_terminal.valid_record_receipt` (and the collection receipt projection) to accept `operation: discard`, `outcome: discarded`, absent item hashes and a null `audit_correlation`; red-first tests that the compact terminal projects the receipt fields and `affected_paths` for a discard, that `record_governance.project_mutation_receipt` does not withhold it, and that `writer_lease` treats it as a receipt.
  Landed as `mutation_terminal._valid_discard_receipt`, admitted by an explicit
  branch rather than by widening the committing operations' outcome set, so
  `outcome: discarded` stays impossible for an append or an update and the
  create/append/update shapes are untouched. Its key set is exact
  (`_record_receipt`, `receipt_version`, `operation`, `collection_id`, `held_id`,
  `affected_paths`, `outcome`, `audit_correlation`) with `held_id` a normalized
  UUID, `audit_correlation` null and exactly one affected path. `held_id` was
  added to `mutation_terminal._RECORD_RECEIPT_FIELDS`, to
  `record_governance.project_mutation_receipt`'s allow-list and to the
  `record_mutation` egress projector; only a discard carries the key, and without
  it the compact response named nothing it had removed.
  Red before the fix: `valid_record_receipt` False, compact `KeyError: 'operation'`,
  `project_mutation_receipt` → `{'withheld': True, 'reason': 'invalid_record_receipt'}`,
  `writer_lease.valid_collection_receipt` False. Tests:
  `test_a_discard_receipt_is_a_valid_record_receipt`,
  `test_the_compact_terminal_projects_a_discard_receipt`,
  `test_governance_does_not_withhold_a_discard_receipt`,
  `test_writer_lease_treats_a_discard_receipt_as_a_receipt`.
- [x] 7.4 MINOR — coverage counts are exact: count held files from the directory listing independent of the reference bound; add `coverage.unreadable` for files that fail to load; red-first test with more held files than the listing bound reports the true count and `unreadable: 0`; a corrupt held file reports `unreadable: 1` without lowering `held` below the readable ones.
  Landed as `records.held_census(...) -> HeldCensus(held, unreadable, candidates)`,
  which replaces `records.held_candidates`: the count comes from the directory
  listing after the release filter and is never capped, while `_MAX_HELD_LOADS`
  (500, formerly `_MAX_HELD_LISTING`) now bounds only how many files one census
  opens. A file that is opened and does not load is counted in `unreadable`
  instead of vanishing. `_inspection_coverage` reconstructs the new key and
  requires `0 <= unreadable <= held`.
  Red before the fix: 505 held files reported `held: 500`; a corrupt file reported
  `held: 1` for two files on disk. Tests:
  `test_coverage_counts_every_held_file_beyond_the_listing_bound` (505 files →
  `held: 505`, `unreadable: 0`, 20 references) and
  `test_a_held_file_that_cannot_be_loaded_is_reported_not_hidden`
  (`held: 2`, `unreadable: 1`, one reference).
- [x] 7.5 MINOR — non-finite floats are holdable: tagged JSON encoding in the held body with restoration on resume; red-first test that a `SCHEMA_FIELD_TYPE` refusal on `NaN` holds and resumes.
  Landed as `records._encode_held_value` / `_decode_held_value` with the tag
  `{"__float__": "NaN"}` and the two infinities; `json.dumps(allow_nan=False)`
  stays as the last line of defence because nothing non-finite survives the
  encoding. Decoding is exact-shape (a one-key mapping whose value is one of the
  three literals), so ordinary candidate data is untouched.
  Red before the fix: the `ValueError` was swallowed by `_HOLD_FAILURES`, so the
  refusal came back with `HELD_CANDIDATE_NOT_WRITTEN` and no held directory.
  Test `test_a_non_finite_number_is_held_and_resumes`: the hold stores the tagged
  value; resuming with no override refuses again on the same field under the same
  `held_id`, which proves the value was restored rather than dropped; resuming
  with a numeric override commits and removes the file.
- [x] 7.6 MINOR — `hold=false` with `held=` refuses naming the argument in `record_memory._validate_arguments`; red-first matrix test.
  Landed in `record_memory._validate_arguments` for `append` and `update`:
  `INVALID_RECORD_ARGUMENTS: ... hold=false cannot be combined with held`. It is
  an argument-matrix rule, not a schema change, so the tool-surface fingerprint
  does not move (below). Test
  `test_declining_the_hold_while_resuming_is_refused_by_name`, parametrised over
  both actions.
- [x] 7.7 MINOR — inventory: docstring states the bounded census; held count never parses candidate payloads; committed count taken without materialising item values where the adapter snapshot allows; red-first test that `inventory_collections` with `references` off does not read held file bodies (monkeypatched loader asserts zero calls).
  Landed: `_collection_coverage` passes `load=references` into `held_census`, so
  the count-only surface lists the directory and opens nothing. The docstring now
  states that it returns no item contents but that the committed count is a real
  census of each releasable collection's adapter snapshot, and that a collection
  it cannot read reports `committed: None`.
  Deviation recorded: the committed count still materialises item values. No
  adapter offers a count-only read — `CollectionAdapter.read()` takes no
  arguments and always returns a full `AdapterSnapshot` — so "where the adapter
  snapshot allows" is nowhere today; the docstring names the cost instead of
  hiding it, and a cheaper read is a separate change to the adapter protocol.
  Red before the fix: the spy recorded one `_load_held_candidate` call and the
  docstring still claimed it opened no canonical item data. Tests:
  `test_a_count_only_inventory_never_reads_a_held_body` (monkeypatched loader,
  zero calls) and `test_the_inventory_docstring_states_the_bounded_census`.
- [x] 7.8 MINOR — `SCHEMA_UNKNOWN_FIELD` diagnostics summary states a count of undeclared fields, not their names; red-first test replaces the current name-echo assertion.
  Landed in `record_governance._diagnostics_summary`: the summary for that code is
  `SCHEMA_UNKNOWN_FIELD: N undeclared fields`, counting every undeclared-field
  issue in the diagnostics rather than appending a `(+N more)` tail that would
  have named the rest by omission. Every other code keeps the shipped
  `CODE: field (+N more)` shape. The name-echo assertion in
  `test_inspect_reports_coverage_for_committed_and_held` is replaced by an
  equality on the new summary plus `"unlisted_channel" not in json.dumps(coverage)`.
- [x] 7.9 NIT — whole-candidate round-trip failures report `details.scope: "frontmatter"` and omit `details.field`; red-first test.
  Landed as `_Issue.scope` plus `records._unrepresentable_candidate`, used by both
  whole-block round-trip failures. `_Issue.as_detail` and `_issue_refusal` now emit
  `field` only when there is one, and `scope` only when set, so every existing
  field-addressed refusal is byte-identical.
  Test `test_a_whole_candidate_round_trip_failure_reports_the_frontmatter_scope`
  (monkeypatched serializer producing unparseable frontmatter, `hold=false` so the
  refusal shape is what is under test).
- [x] 7.10 Hygiene — `uvx ruff check --select I tests/test_record_public_surface.py` clean (import block sorted); a test states the intended behaviour that two candidates sharing a natural key re-hold onto one file.
  The `from exomem import ...` block is now one sorted parenthesised import;
  `uvx ruff check --select I tests/test_record_public_surface.py` → All checks
  passed. `test_two_candidates_sharing_a_natural_key_re_hold_onto_one_file`
  states the identity behaviour: two candidates differing in `word_count` but
  sharing `published_on` + `slug` produce one `held_id`, one file, and the
  second candidate's payload.
- [x] 7.11 Recheck evidence — scoped suites green under the state-root override; the files newly touched by this round are listed here for the reviewer's recheck.
  Scoped suites under `XDG_STATE_HOME=<scratch>`: **929 passed, 0 failed, 0
  errors** (baseline before this round on the same list: 911 passed) —
  `tests/test_record_mutation.py tests/test_record_governance.py
  tests/test_record_memory_command.py tests/test_record_public_surface.py
  tests/test_record_collection_discovery.py tests/test_record_representation_matrix.py
  tests/test_record_audit_protocol.py tests/test_plan_memory_command.py
  tests/test_mutation_terminal.py tests/test_writer_lease.py
  tests/test_governance_egress.py tests/test_mcp_schema_fidelity.py
  tests/test_tool_surface_fingerprint.py tests/test_tool_annotations.py
  tests/test_scaffold_no_leak.py tests/test_workflow_skills.py`.
  **Tool-surface fingerprint did not move this round**: still
  `d1e78d2efae5a0129adc90226af53c80750ca35926eeef5184145c8c038b9eae`, 29 tools.
  The argument-matrix refusal (7.6) is a validation rule, not a schema change, so
  nothing was regenerated.
  Files newly touched by this correction round:
  - `src/exomem/records.py` (7.1, 7.2, 7.4, 7.5, 7.9)
  - `src/exomem/record_formats.py` (7.2) — first change to this file in the change
  - `src/exomem/record_governance.py` (7.3, 7.4, 7.7, 7.8)
  - `src/exomem/record_memory.py` (7.6)
  - `src/exomem/mutation_terminal.py` (7.3) — first change to this file in the change
  - `tests/test_record_mutation.py` (7.2, 7.3, 7.5, 7.9, 7.10)
  - `tests/test_record_governance.py` (7.4, 7.7, 7.8)
  - `tests/test_record_memory_command.py` (7.6)
  - `tests/test_record_public_surface.py` (7.10)
  - `tests/test_plan_memory_command.py` (7.1)
  - `openspec/changes/repair-records-writer-representation-and-held-records/tasks.md`
  API note for the recheck: `records.held_candidates` (recorded under 3.6) is
  replaced by `records.held_census`, which returns the exact count, the unreadable
  count and the loaded candidates together; the disclosure filtering it documented
  is unchanged and `test_a_withheld_held_candidate_contributes_neither_reference_nor_count`
  still pins it.

## 8. Recheck corrections (independent review round 2, APPROVE with two new minors)

- [x] 8.1 MINOR — markdown-log emptiness rules (empty heading string, empty or whitespace-only note) are collected as representability issues with field paths and hold, like the grammar tokens. Red first: `test_an_empty_heading_value_is_addressed_and_held` and `test_a_whitespace_only_note_is_addressed_and_held` failed with `KeyError: 'field'` (the render layer refused with empty details and no hold); green after `_collect_log_grammar_issues` gained the two rules. Render-layer checks unchanged.
- [x] 8.2 MINOR — held-body tag collision: a candidate object shaped like a tag (`{"__float__": …}` or `{"__escaped__": …}`) is wrapped under `__escaped__` on encode and unwrapped literally on decode. Red first: `test_a_literal_tag_shaped_object_survives_the_held_round_trip[float-tag|escape-tag|escape-scalar]` failed on "a tag-shaped literal must be escaped in the held body"; green after `_encode_held_value`/`_decode_held_value` gained the reserved-shape rule; the resumed item reads back the literal object.
- [x] 8.3 Evidence — `tests/test_record_mutation.py tests/test_record_governance.py`: 219 passed, 0 failed, 0 errors under the state-root override; `uvx ruff check src/exomem/records.py tests/test_record_mutation.py` clean. Files touched this round: `src/exomem/records.py`, `tests/test_record_mutation.py`, this file and `design.md`.

## 6. Verification

- [x] 6.1 End-to-end journey through the MCP-facing `record_memory` surface on a fresh fixture vault with a genericised ledger collection (no personal names): create → append with a multi-line required string → query returns it → inspect shows `coverage.committed: 1, held: 0` → append with an undeclared field refuses and holds → inspect shows `held: 1` with the reference → resume with an override commits → inspect shows `committed: 2, held: 0` and the audit head advanced exactly twice.
  Landed as `test_the_whole_journey_runs_through_the_record_memory_surface`
  (`tests/test_record_mutation.py`), on a fresh `tmp_path` vault with the generic
  publications-ledger collection from `tests/record_fixtures.py` (no personal
  names, no accounts, no people). Every step goes through `record_memory(...)`:
  create, append with a two-line-break required string, query (the row reads back
  the exact string), inspect `coverage.committed: 1, held: 0`, an append with an
  undeclared field refusing with `SCHEMA_UNKNOWN_FIELD` and a `held` reference,
  inspect `held: 1` naming that reference, resume with `held=` plus a null override,
  inspect `committed: 2, held: 0` with no references, and three distinct audit
  heads (create, then exactly two commits).
- [x] 6.2 Gates: scoped suites `tests/test_record_*.py tests/test_structured_*.py tests/test_plan*.py tests/test_tool_surface_fingerprint.py tests/test_mcp_schema_fidelity.py tests/test_scaffold_no_leak.py` green during iteration; at completion the full suite (`PYTHONPATH=src EXOMEM_DISABLE_EMBEDDINGS=1 uv run python -m pytest -q`), `uv run ruff check`, mypy as CI runs it, `openspec validate --all --strict`, and `uv run python scripts/validate-public-artifacts.py --repository` all pass; record counts here.
  Recorded 2026-09-10 on the final bytes. Full suite run in the CI shard layout
  (12 `EXOMEM_TEST_TIER=core` + 4 `harness` pytest-split shards, `-o timeout=180`
  because the machine carried another repository's integration suite at load
  average above 20): 17,815 passed, 60 failed, 8 errors. The 68 failing ids were
  rerun on the branch base 841910d4: 51 fail there too (permission-safety tests
  refusing the 0775 worktree under a 0002 umask, installer and benchmark tests
  needing `bun`/`netstat`, hosted-platform signing inputs); the 17 branch-only
  failures were stale derived artifacts, fixed by `exomem package-skills
  --plugin-root plugins/claude-code`, `scripts/hosted-plugin.py render`, and
  `render --candidate hosted-alpha-agent-v5` (same resync as the last plugin-skill
  change on main), plus one load-flaky foreground-activity test that passes on
  rerun; after the fixes the branch-only set is empty. Post-regeneration scoped
  rerun of the Records, structured-collection, planning, tool-surface, MCP-schema,
  scaffold-leak and workflow-skill suites: 410 passed, 0 failed. `uvx ruff check .
  --select F` clean; CI full-ruff file list clean; `uvx mypy` CI file list: no
  issues in 14 files; `openspec validate --all --strict` with the CI pin 1.10.0:
  191 passed, 0 failed (local 1.12.0 adds 79 pre-existing Purpose-placeholder
  warnings on main specs, none from this change); archive discipline OK;
  `validate-public-artifacts.py --repository`: 4022 files clean.
- [ ] 6.3 (optional, post-deploy, operational) On the live vault through `record_memory`: append the 2026-09-09 public reply into the canonical outbound-social collection with real line breaks; update the 2026-09-08 item's escaped `exact_text`; inspect reports five committed and zero held. Not code; recorded for closure evidence only.
