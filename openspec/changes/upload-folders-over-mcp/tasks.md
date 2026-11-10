## 1. The route

- [x] 1.1 Add the lane `import` to `upload_files` and the `next` steps to its reply, with the media allowance left uncharged, and refuse the lane on Exomem Cloud. **Evidence:** `tests/test_import_upload.py` journey (parses and runs the reply's own `curl` line on the archive that the guide teaches) and `test_the_import_lane_is_refused_on_exomem_cloud`; `tests/test_media_store.py::test_a_ticket_upload_is_admitted_from_the_recorded_census_and_refused_only_on_a_confirmed_figure`, red when the lane charges the allowance.
- [x] 1.2 Add `upload` to `imports.file`, refuse it with `path`, and derive the stage from the ticket. **Evidence:** `test_a_repeated_first_call_resumes_the_same_import_and_unpacks_nothing_again` and `test_an_upload_and_a_path_together_are_refused`.
- [x] 1.3 Unpack plain tar, gzip tar and zip safely in one module, choosing the format from the first bytes, refusing each unsafe member by name and every compression without an output bound, and leaving no stage. **Evidence:** `test_an_archive_that_breaks_a_limit_is_refused_by_name_and_leaves_no_stage` (eighteen cases, including bzip2, xz, case aliases, oversized headers and names the volume cannot store), `test_headers_that_are_each_under_the_limit_but_pass_it_together_are_refused`, `test_a_plain_tar_that_holds_a_zip_imports_every_member`, `test_a_gzip_tar_split_into_members_imports_every_file` and `test_a_gzip_stream_that_runs_on_after_the_end_block_is_not_read_to_its_end`.
- [x] 1.4 Stop for free space and keep the hold, saying so only when the hold is back. **Evidence:** `test_an_archive_that_does_not_fit_stops_the_import_and_unpacks_once_there_is_room` and `test_a_stop_for_space_says_the_archive_was_not_kept_when_its_hold_ran_out`.
- [x] 1.5 Run on a platform without `os.statvfs` and `os.sync`, and tell the agent when a stage has no record that vouches for it. **Evidence:** `test_the_import_runs_on_a_platform_without_statvfs_or_sync`, `test_the_import_flushes_what_it_wrote_and_not_the_whole_host` and `test_a_stage_that_no_record_vouches_for_tells_the_agent_its_copies_are_kept`.

## 2. Guidance and artifacts

- [x] 2.1 Teach the route once in `read_instructions`, and point to it from the import guide, the operations and routing references, the `exomem-ingest` skill and `docs/deployment.md`. **Evidence:** `tests/test_workflow_skills.py`, `tests/test_scaffold_no_leak.py`.
- [x] 2.2 Regenerate the skill contract, capabilities, tool schemas, plugin trees and the pending connector digest. **Evidence:** `scripts/cloud-plugin.py check`, `scripts/generate-capabilities.py --check`, `tests/test_mcp_schema_fidelity.py`, `tests/test_connector_guardrails.py`, `tests/test_tool_schema_budget.py`.

## 3. Delivery

- [ ] 3.1 Independent review of the finished change, merge, release, and archive this change. **Evidence:** the review verdict on the PR, the merge commit and the release tag.
