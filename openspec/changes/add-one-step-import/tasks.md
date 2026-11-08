## 1. The `file` action

- [x] 1.1 Add `adoption_run.file`: a driver that picks the next step from the newest page's saved phase and calls the existing `start`, `select`, `plan`, `apply`, `finish` and `cancel`. **Evidence:** `tests/test_adoption_file.py` journey and two-driver cases.
- [x] 1.2 Expose it as `adoption_studio` action `file` and `import_memory` kind `file`, with the governance egress class and the compact terminal keys. **Evidence:** `tests/test_tool_surface_contract.py`, `tests/test_governance_egress.py`, `tests/test_mcp_schema_fidelity.py`.
- [x] 1.3 Reconcile an interrupted `file` call under its original identity: repeat only its stored `apply`, or run none. **Evidence:** the three crash cuts in `tests/test_adoption_file.py` and `tests/test_import_recovery_mcp.py`.
- [x] 1.4 Retry a page's failed files once, then finish the page, or close it when every file failed, and go on to the next page. **Evidence:** `test_a_page_whose_files_fail_retries_them_once_and_the_import_goes_on_to_the_later_pages`, red with the retry and close steps removed.
- [x] 1.5 Let a call with only the path answer `done` from a finished import, with a residual or with its stage removed. **Evidence:** `test_a_call_with_only_the_path_after_done_answers_done_again_and_starts_no_run`, red with the old resume rule.
- [x] 1.6 Refuse a call with only the path that names another collection, and name the collection in each reply. **Evidence:** `test_a_call_with_only_the_path_that_names_another_collection_is_refused_with_the_imports_own`, red without the check.
- [x] 1.7 Keep the import's totals in `progress` and `summary`, and put the last `finish` report under `filed.last_finished_page` with its page number. **Evidence:** `test_a_page_with_nothing_eligible_does_not_stop_the_import`, red with the old top-level coverage.

## 2. The Cloud CLI

- [x] 2.1 File each landed window while the series waits and at the end; add `--no-file` and `--collection`. **Evidence:** the filing cases in `tests/test_cloud_import_windows.py`.
- [x] 2.2 Treat only a refusal of the action as an older cell; stop one window with the cell's code and reason on any other error; repeat the exact call after an unknown or pending outcome. **Evidence:** `test_a_file_call_that_the_cell_refuses_with_an_error_stops_that_window_with_the_cells_code`, red when every error reads as an older cell.
- [x] 2.3 Count a moved page position as progress in the stall check, and skip windows whose filing the status reports done on a rerun. **Evidence:** `import_windows` reports `filing_done`; `tests/test_adoption_windows.py` and `tests/test_cloud_import_windows.py` pass.

## 3. Guidance and artifacts

- [x] 3.1 Name the one call in `exomem import`, the import and operations references and the `exomem-ingest` skill, and tell the agent to report the summary totals and the residual. **Evidence:** `tests/test_local_import.py`, `tests/test_workflow_skills.py`, `tests/test_scaffold_no_leak.py`.
- [x] 3.2 Regenerate the skill contract, the plugin trees and the tool schemas, and raise the schema budget with its reason. **Evidence:** `scripts/refresh-skill-contract.py --check`, `scripts/cloud-plugin.py check`, `tests/test_tool_schema_budget.py`, `tests/test_plugin_sync.py`.
- [x] 3.3 File the Cloud rehearsal's large import with `file` calls. **Evidence:** `infra/cloud-rehearsal/tests/test_large_import_run.py`.

## 4. Proof and delivery

- [ ] 4.1 Run one Cloud rehearsal with `file`.
- [ ] 4.2 Run the real-model import-guidance probe (cost stated first).
- [ ] 4.3 Independent review of the finished change, with each finding fixed or withdrawn.
- [ ] 4.4 Merge, release, and archive this change.
