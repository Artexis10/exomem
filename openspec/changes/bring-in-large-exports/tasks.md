## 1. Archive members (B)

- [ ] 1.1 Move the zip checks from `hosted_transfer_routes.py` into `archive_members.py`; adoption staging keeps its behaviour.
- [ ] 1.2 Preserve `archive=members` into the member pool and manifest, with `already_stored` for a recorded archive.
- [ ] 1.3 Prove: real-shaped invented archive expands, re-expands with no new blob, and hostile archives write nothing.

## 2. Upload sessions (A, local and public listeners)

- [ ] 2.1 `upload_sessions.py` and the `/upload/sessions` routes, with the listener allowlist change.
- [ ] 2.2 `exomem attach` resumes through a session.
- [ ] 2.3 Prove: an interrupted upload resumes through the real listener and worker; a hash mismatch, a wrong secret, cancel and expiry leave no bytes.

## 3. Import grammar (C)

- [x] 3.1 Member sources with member-and-row checkpoints and the `import_members` skip.
  - Evidence: `tests/test_collection_store_import_exports.py`: the export journey (red when the skip never fires: 3 members read, not 1), the member hash test (red without the hash check: 1 row imported) and the reimport test (red when reimport is ignored: 0 members read).
- [x] 3.2 `json-document` rows with an `ijson` reader, ancestor paths, `$index`, `$value`, literals and scale.
  - Evidence: `tests/test_collection_store_import_exports.py` late-ancestor test (red when a late ancestor is captured) and mapping-validation test (red when a zone may omit fold); `tests/test_collection_store_importer.py` peak-memory test, json-document case (red at 39 MiB traced when the reader loads the file).
- [x] 3.3 Time bases with zones, offsets, increments, clock, fold and gap; pin `tzdata`.
  - Evidence: `tests/test_collection_store_import_exports.py`: the export journey (red when the fold state leaves the checkpoint: 8 imported, 1 duplicate; red when a gap row stops the job) and the time-basis test (red when elapsed and wall clocks swap).
- [ ] 3.4 Prove: an installed-MCP journey over an invented multi-member export that crosses the autumn fold and the spring gap, with a restart mid-member and a second job that skips imported members.
  - Open: `tests/test_collection_store_import_exports.py` runs this journey in process through `commands.op_record_memory` on a hand-built member family. The installed-wheel run over the real archive expansion belongs to the integrated batch.

## 4. Derived import collections (D)

- [ ] 4.1 Implement with the replica publisher batch (S1.2b): derived marking, `COLLECTION_DERIVED` refusals, replica exclusion, import log and rebuild.
