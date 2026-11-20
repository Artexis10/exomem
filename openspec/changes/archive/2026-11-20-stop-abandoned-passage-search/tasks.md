# Tasks

## 1. Stop abandoned passage requests

- [x] 1.1 Check the serving host's cancel scope before eligibility, query encoding, chunk selection and byte verification. Verify: a test that abandons a passage request during eligibility sees no query encoding.
- [x] 1.2 Log `passage_search_abandoned` with the phase the request stopped before. Verify: the cell log of a one-second abandon run holds these events.
- [x] 1.3 Measure one and four callers, idle and with a client that abandons at one second, in the production envelope before and after the change.

## 2. Closure

- [x] 2.1 Synchronize the delta into the canonical spec and archive this change with `openspec archive`. Verify: `openspec validate --all --strict` passes before and after the archive.
