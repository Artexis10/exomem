## 1. Archive members (B)

- [x] 1.1 Move the zip checks from `hosted_transfer_routes.py` into `archive_members.py`; adoption staging keeps its behaviour.
  Evidence: `tests/test_hosted_adoption_staging.py` and `tests/test_hosted_transfer_v2.py` stay green; `test_a_hostile_archive_writes_nothing[zip-slip]` turns red when the shared `..` check is removed.
- [x] 1.2 Preserve `archive=members` into the member pool and manifest, with `already_stored` for a recorded archive.
  Evidence: `test_archive_members_upload_stores_each_distinct_member_once` turns red when the recorded-archive lookup never matches; `test_preserving_a_recorded_archive_again_restores_a_lost_blob_and_nothing_else` turns red (0 restored) when `already_stored` skips the restore.
- [x] 1.3 Prove: real-shaped invented archive expands, re-expands with no new blob, and hostile archives write nothing.
  Evidence: the same test turns red when blobs are pooled per archive; each `test_a_hostile_archive_writes_nothing` case turns red when its own check is removed.

## 2. Upload sessions (A, local and public listeners)

- [x] 2.1 `upload_sessions.py` and the `/upload/sessions` routes, with the listener allowlist change.
  Evidence: `test_local_ingress_e2e.py` turns red when the local allowlist drops `/upload/sessions/` or `HEAD` reports the declared length; `test_local_refusal_*` turn red when that prefix is dropped or widened to `/upload/`.
- [x] 2.2 `exomem attach` resumes through a session.
  Evidence: `test_attach_resumes_an_interrupted_archive_upload_on_its_next_run` turns red when the CLI ignores its saved session record or writes it group-readable.
- [x] 2.3 Prove: an interrupted upload resumes through the real listener and worker; a hash mismatch, a wrong secret, cancel and expiry leave no bytes.
  Evidence: the e2e turns red when the running digest restarts per part; `tests/test_upload_sessions.py` cases turn red when a mismatch keeps the `.part`, a wrong secret answers 403, or a cancel or an expiry keeps the `.part`.

## 3. Import grammar (C)

- [x] 3.1 Member sources with member-and-row checkpoints and the `import_members` skip.
  - Evidence: `tests/test_collection_store_import_exports.py`: the export journey (red when the skip never fires: 3 members read, not 1), the member hash test (red without the hash check: 1 row imported) and the reimport test (red when reimport is ignored: 0 members read).
  - Evidence: the journey asserts each member's import log row (counts, digest, row count) and `test_a_crash_at_a_members_end_logs_it_exactly_once_on_resume` checks one log row after a crash at a member's end; both turn red (a 0/0 log row) when the open member's tally leaves the checkpoint, and the crash test when the log row commits in its own transaction. `tests/test_collection_store_schema.py` turns red when `import_members` loses its append-only triggers.
- [x] 3.2 `json-document` rows with an `ijson` reader, ancestor paths, `$index`, `$value`, literals and scale.
  - Evidence: `tests/test_collection_store_import_exports.py` late-ancestor test (red when a late ancestor is captured) and mapping-validation test (red when a zone may omit fold); `tests/test_collection_store_importer.py` peak-memory test, json-document case (red at 39 MiB traced when the reader loads the file).
- [x] 3.3 Time bases with zones, offsets, increments, clock, fold and gap; pin `tzdata`.
  - Evidence: `tests/test_collection_store_import_exports.py`: the export journey (red when the fold state leaves the checkpoint: 8 imported, 1 duplicate; red when a gap row stops the job) and the time-basis test (red when elapsed and wall clocks swap).
- [x] 3.4 Prove: an agent-route journey over invented multi-member export zips that the real archive expansion preserves, crossing the autumn fold and the spring gap, with a restart mid-member and a second job that skips imported members.
  - Evidence: `test_an_export_imports_across_the_fold_and_gap_resumes_and_skips_imported_members` preserves each zip through `archive_members.preserve_members` and imports through `commands.op_record_memory`. It turns red (`IMPORT_MANIFEST_INVALID`) when expansion records each blob path relative to the vault instead of the family. It runs in process: no installed-wheel harness covers collection imports yet.

## 4. Derived import collections (D)

- [ ] 4.1 Store schema: revision 9 adds the append-only import log and the `import_member` receipt; a later revision adds the `derived` flag and its trigger.
  - Partly: revision 9 creates the append-only import log, and each logged member records an `import_member` control transition (evidence under 3.1). The `derived` flag and its trigger remain.
- [ ] 4.2 `collection_store/derived_rows.py`: the derived file, its layout, the two-connection write order, `reconcile` and rebuild; the importer's derived apply path.
- [ ] 4.3 Query readers attach the derived file through a per-collection row source; `QUERY_REBUILDING` while a rebuild runs.
- [ ] 4.4 `COLLECTION_DERIVED` refusals, summary counts from the log, `--include-derived` backup, and the reserved-path entry.
- [ ] 4.5 Prove: an installed-MCP import, then deleting the derived file and restarting, rebuilds rows equal by count and digest while rollups answer throughout; the replica and export hold no derived rows; a crash at each commit boundary recovers without duplicates; a missing blob refuses instead of returning zero.
