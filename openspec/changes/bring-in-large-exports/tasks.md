## 1. Archive members (B)

- [x] 1.1 Move the zip checks from `hosted_transfer_routes.py` into `archive_members.py`; adoption staging keeps its behaviour.
  Evidence: `tests/test_hosted_adoption_staging.py` and `tests/test_hosted_transfer_v2.py` stay green; `test_a_hostile_archive_writes_nothing[zip-slip]` turns red when the shared `..` check is removed.
- [x] 1.2 Preserve `archive=members` into the member pool and manifest, with `already_stored` for a recorded archive.
  Evidence: `test_archive_members_upload_stores_each_distinct_member_once` turns red when the recorded-archive lookup never matches.
- [x] 1.3 Prove: real-shaped invented archive expands, re-expands with no new blob, and hostile archives write nothing.
  Evidence: the same test turns red when blobs are pooled per archive; each `test_a_hostile_archive_writes_nothing` case turns red when its own check is removed.

## 2. Upload sessions (A, local and public listeners)

- [ ] 2.1 `upload_sessions.py` and the `/upload/sessions` routes, with the listener allowlist change.
- [ ] 2.2 `exomem attach` resumes through a session.
- [ ] 2.3 Prove: an interrupted upload resumes through the real listener and worker; a hash mismatch, a wrong secret, cancel and expiry leave no bytes.

## 3. Import grammar (C)

- [ ] 3.1 Member sources with member-and-row checkpoints and the `import_members` skip.
- [ ] 3.2 `json-document` rows with an `ijson` reader, ancestor paths, `$index`, `$value`, literals and scale.
- [ ] 3.3 Time bases with zones, offsets, increments, clock, fold and gap; pin `tzdata`.
- [ ] 3.4 Prove: an installed-MCP journey over an invented multi-member export that crosses the autumn fold and the spring gap, with a restart mid-member and a second job that skips imported members.

## 4. Derived import collections (D)

- [ ] 4.1 Implement with the replica publisher batch (S1.2b): derived marking, `COLLECTION_DERIVED` refusals, replica exclusion, import log and rebuild.
