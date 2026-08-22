## 1. Unicode Page Identity

- [x] 1.1 Add failing unit tests for Unicode/YAML-significant titles, explicit ASCII slugs, invalid slug rejection, and legacy fallback behavior.
- [x] 1.2 Implement shared title resolution, YAML-safe title serialization, explicit slug validation, and lossy automatic-slug warnings.
- [x] 1.3 Wire title and slug behavior through note/add/link/preserve/adopt leaves and the renamed product commands without renaming existing files.
- [x] 1.4 Add surface-level schema/CLI/REST tests proving optional slugs and title behavior stay consistent.

## 2. Index And Import Integrity

- [x] 2.1 Add failing tests for complete top-level totals, missing-row insertion, Sources sub-index reconciliation, and colon-bearing imported paths.
- [x] 2.2 Implement complete Sources/Notes/Entities count refresh in normal writers and reconcile while preserving curated index text.
- [x] 2.3 Serialize adoption/import provenance with shared YAML-safe scalar handling.

## 3. Transactional Vault Mutations

- [x] 3.1 Add failure-injection tests for mid-replacement batch rollback and move/link-rewrite rollback.
- [x] 3.2 Implement rollback-safe batch replacement with clear compound-error reporting if restoration also fails.
- [x] 3.3 Rework move_file into a reversible rename plus rollback-safe inbound-link batch and defer sidecar notifications until commit.

## 4. Diagnostics And Hook Health

- [x] 4.1 Add tests for lean/hybrid/media doctor inference without model loading and explicit-profile precedence.
- [x] 4.2 Implement capability-based doctor inference using dependency/executable discovery only.
- [x] 4.3 Add tests for skipped absent clients, strict explicitly selected clients, and failing partial hook installations.
- [x] 4.4 Implement skipped-client reporting and aggregate success semantics in install-hook --check.

## 5. Documentation And Verification

- [x] 5.1 Update generic scaffold/reference docs for 100-character slugs, Unicode display titles, explicit ASCII slugs, canonical resolution, and no automatic renames.
- [x] 5.2 Run focused tests, ruff, OpenSpec validation, scaffold leak guard, and the full lean pytest suite; fix regressions.
- [x] 5.3 Review the final diff for compatibility and scope, mark all OpenSpec tasks complete, and commit the isolated branch.
