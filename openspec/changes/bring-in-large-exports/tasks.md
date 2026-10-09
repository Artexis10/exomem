## 1. Archive members (B)

- [ ] 1.1 Move the zip checks from `hosted_transfer_routes.py` into `archive_members.py`; adoption staging keeps its behaviour.
- [ ] 1.2 Preserve `archive=members` into the member pool and manifest, with `already_stored` for a recorded archive.
- [ ] 1.3 Prove: real-shaped invented archive expands, re-expands with no new blob, and hostile archives write nothing.

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
