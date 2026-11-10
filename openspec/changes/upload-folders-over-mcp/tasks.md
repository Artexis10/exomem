## 1. The route

- [x] 1.1 Add the lane `import` to `upload_files` and the `next` steps to its reply, with the media allowance left uncharged. **Evidence:** `tests/test_import_upload.py` journey (reads the token from `next`); `tests/test_media_store.py::test_a_ticket_upload_is_admitted_from_the_recorded_census_and_refused_only_on_a_confirmed_figure`, red when the lane charges the allowance.
- [x] 1.2 Add `upload` to `imports.file`, refuse it with `path`, and derive the stage from the ticket. **Evidence:** `test_a_repeated_first_call_resumes_the_same_import_and_unpacks_nothing_again` and `test_an_upload_and_a_path_together_are_refused`.
- [x] 1.3 Unpack tar and zip safely in one module and refuse each unsafe member by name, leaving no stage. **Evidence:** `test_an_archive_that_breaks_a_limit_is_refused_by_name_and_leaves_no_stage`, ten cases.
- [x] 1.4 Stop for free space and keep the hold. **Evidence:** `test_an_archive_that_does_not_fit_stops_the_import_and_unpacks_once_there_is_room`.

## 2. Guidance and artifacts

- [x] 2.1 Teach the route in `read_instructions`, the import guide, the operations and routing references, the `exomem-ingest` skill and `docs/deployment.md`. **Evidence:** `tests/test_workflow_skills.py`, `tests/test_scaffold_no_leak.py`.
- [x] 2.2 Regenerate the skill contract, capabilities, tool schemas, plugin trees and the pending connector digest. **Evidence:** `scripts/cloud-plugin.py check`, `scripts/generate-capabilities.py --check`, `tests/test_mcp_schema_fidelity.py`, `tests/test_connector_guardrails.py`, `tests/test_tool_schema_budget.py`.

## 3. Delivery

- [ ] 3.1 Independent review of the finished change, merge, release, and archive this change. **Evidence:** the review verdict on the PR, the merge commit and the release tag.
