## 1. Archive members (B)

- [x] 1.1 Move the zip checks from `hosted_transfer_routes.py` into `archive_members.py`; adoption staging keeps its behaviour.
  Evidence: `tests/test_hosted_adoption_staging.py` and `tests/test_hosted_transfer_v2.py` stay green; `test_a_hostile_archive_writes_nothing[zip-slip]` turns red when the shared `..` check is removed.
- [x] 1.2 Preserve `archive=members` into the member pool and manifest, with `already_stored` for a recorded archive.
  Evidence: `test_archive_members_upload_stores_each_distinct_member_once` turns red when the recorded-archive lookup never matches; `test_preserving_a_recorded_archive_again_restores_a_lost_blob_and_nothing_else` turns red (0 restored) when `already_stored` skips the restore.
- [x] 1.3 Prove: real-shaped invented archive expands, re-expands with no new blob, and hostile archives write nothing.
  Evidence: the same test turns red when blobs are pooled per archive; each `test_a_hostile_archive_writes_nothing` case turns red when its own check is removed.
  - Evidence (review round 2): `test_an_oversized_central_directory_is_refused_before_it_is_loaded` turns red (`ARCHIVE_INVALID`) when the end-record check is skipped; the `undecodable-name` case turns red (`UnicodeDecodeError`) without its mapping.
  - Evidence (review round 2): `test_an_expansion_removes_the_temp_a_killed_expansion_left_in_the_pool` turns red without the pool sweep; `test_an_expansion_needs_room_only_for_the_blobs_it_writes` turns red with the up-front sum restored and with the per-blob check removed.

## 2. Upload sessions (A, local and public listeners)

- [x] 2.1 `upload_sessions.py` and the `/upload/sessions` routes, with the listener allowlist change.
  Evidence: `test_local_ingress_e2e.py` turns red when the local allowlist drops `/upload/sessions/` or `HEAD` reports the declared length; `test_local_refusal_*` turn red when that prefix is dropped or widened to `/upload/`.
- [x] 2.2 `exomem attach` resumes through a session.
  Evidence: `test_attach_resumes_an_interrupted_archive_upload_on_its_next_run` turns red when the CLI ignores its saved session record or writes it group-readable.
  - Evidence (review round 2): `test_attach_sends_a_file_the_listener_refuses_as_too_large_through_a_session` turns red when a 413 does not fall back to a session; `test_attach_keeps_an_upload_whose_commit_found_the_disk_full_and_commits_it_on_the_next_run` turns red when a transient failure fails the session or the CLI ignores `retryable`.
- [x] 2.3 Prove: an interrupted upload resumes through the real listener and worker; a hash mismatch, a wrong secret, cancel and expiry leave no bytes.
  Evidence: the e2e turns red when the running digest restarts per part; `tests/test_upload_sessions.py` cases turn red when a mismatch keeps the `.part`, a wrong secret answers 403, or a cancel or an expiry keeps the `.part`. `test_a_part_whose_client_drops_mid_body_keeps_what_arrived_and_the_upload_resumes` drops a part mid-body on a real uvicorn listener; it turns red (offset 40, not 70) when a disconnect discards the buffered bytes or hashes them without writing them.
  - Evidence (review round 2): `test_only_the_serving_runtime_finishes_a_commit_that_a_stop_interrupted` builds each process kind in a real subprocess; standby turns red with the resume at route registration, stdio without the HTTP-app guard, serving without the activation step. `test_a_second_process_never_commits_a_session_already_being_committed` turns red without the commit lock.
  - Evidence (review round 2): `test_a_commit_resumed_after_its_file_landed_answers_already_stored` turns red (`ARTIFACT_EXISTS`) without the stored-artifact lookup; `test_a_session_whose_file_cannot_land_is_refused_before_any_byte_is_sent` turns red without the creation check; `test_concurrent_creates_never_open_more_than_the_per_credential_limit` (12 opened, not 4) without the creation lock; `test_a_cancel_read_before_the_final_part_never_deletes_the_verified_upload` without the re-read; `test_a_number_too_long_to_parse_is_a_bad_request` with the digit bound raised past 4300.

## 3. Import grammar (C)

- [x] 3.1 Member sources with member-and-row checkpoints and the `import_members` skip.
  - Evidence: `tests/test_collection_store_import_exports.py`: the export journey (red when the skip never fires: 3 members read, not 1), the member hash test (red without the hash check: 1 row imported) and the reimport test (red when reimport is ignored: 0 members read).
  - Evidence: the journey asserts each member's import log row (counts, digest, row count) and `test_a_crash_at_a_members_end_logs_it_exactly_once_on_resume` checks one log row after a crash at a member's end; both turn red (a 0/0 log row) when the open member's tally leaves the checkpoint, and the crash test when the log row commits in its own transaction. `tests/test_collection_store_schema.py` turns red when `import_members` loses its append-only triggers.
  - Evidence: `test_a_manifest_without_the_owners_raw_binding_opens_no_member_blob` turns red (the forged start is accepted) when `archive_members.read_manifest` trusts a file without the raw prefix and binding; `test_a_manifest_over_its_size_cap_is_refused` turns red when the reader's read is not capped. Expansion's `already_stored` and the importer share that reader.
  - Evidence: `test_a_member_identical_to_an_earlier_one_in_the_same_job_still_wins_by_path` turns red (read 2, skipped 1) when the skip counts the job's own log rows.
  - Evidence: the journey's fourth log row counts a row the owner added between jobs (34); it turns red (33) when the count is the previous log row's plus the member's accepted rows. The count reads only rows created since the previous log row: a 10x larger synthetic collection took 6.7 ms then 57.6 ms by `COUNT(*)` and 0.3 ms then 0.2 ms by the walk.
- [x] 3.2 `json-document` rows with an `ijson` reader, ancestor paths, `$index`, `$value`, literals and scale.
  - Evidence: `tests/test_collection_store_import_exports.py` late-ancestor test (red when a late ancestor is captured) and mapping-validation test (red when a zone may omit fold); `tests/test_collection_store_importer.py` peak-memory test, json-document case (red at 39 MiB traced when the reader loads the file).
  - Evidence: `test_numbers_beyond_int64_or_a_float_cost_only_their_own_rows` turns red (no rows) when a member the C parser refuses is not read again with the pure-Python one, and red (`OverflowError`) when `_finite` converts an int to a float; `test_a_csv_integer_too_long_to_read_is_a_row_error` turns red (`ValueError`) without its guard; the `zone-not-a-name` mapping case turns red (`TypeError`) when the zone cache sees the name before its type check.
- [x] 3.3 Time bases with zones, offsets, increments, clock, fold and gap; pin `tzdata`.
  - Evidence: `tests/test_collection_store_import_exports.py`: the export journey (red when the fold state leaves the checkpoint: 8 imported, 1 duplicate; red when a gap row stops the job) and the time-basis test (red when elapsed and wall clocks swap).
  - Evidence: `test_fold_order_starts_over_after_each_repeated_hour` (one series over two autumns; two series in one CSV) checks each instant against `zoneinfo`; both cases turn red (the second fold's first pass at +02:00) when the `order` rule keeps the later offset after its repeated hour.
- [x] 3.4 Prove: an agent-route journey over invented multi-member export zips that the real archive expansion preserves, crossing the autumn fold and the spring gap, with a restart mid-member and a second job that skips imported members.
  - Evidence: `test_an_export_imports_across_the_fold_and_gap_resumes_and_skips_imported_members` preserves each zip through `archive_members.preserve_members` and imports through `commands.op_record_memory`. It turns red (`IMPORT_MANIFEST_INVALID`) when expansion records each blob path relative to the vault instead of the family. It runs in process: no installed-wheel harness covers collection imports yet.

## 4. Derived import collections (D)

- [ ] 4.1 Store schema: revision 9 adds the append-only import log and the `import_member` receipt; a later revision adds the `derived` flag and its trigger.
  - Partly: revision 9 creates the append-only import log, and each logged member records an `import_member` control transition (evidence under 3.1). The `derived` flag and its trigger remain.
  - Revision 9's log also records the manifest SHA-256, the importer version and the zone-rules version: the journey turns red when `zone_rules` is not written, and the crash test when the member's SHA-256 stands in for the manifest's.
- [ ] 4.2 `collection_store/derived_rows.py`: the derived file, its layout, the two-connection write order, `reconcile` and rebuild; the importer's derived apply path.
- [ ] 4.3 Query readers attach the derived file through a per-collection row source; `QUERY_REBUILDING` while a rebuild runs.
- [ ] 4.4 `COLLECTION_DERIVED` refusals, summary counts from the log, `--include-derived` backup, and the reserved-path entry.
- [ ] 4.5 Prove: an installed-MCP import, then deleting the derived file and restarting, rebuilds rows equal by count and digest while rollups answer throughout; the replica and export hold no derived rows; a crash at each commit boundary recovers without duplicates; a missing blob refuses instead of returning zero.
