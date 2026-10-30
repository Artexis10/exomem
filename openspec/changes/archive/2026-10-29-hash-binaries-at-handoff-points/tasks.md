## 1. Regression proof

- [x] 1.1 Add a test beside `test_batch_write_rejects_changed_binary_stream_before_publication` in `tests/test_vault.py`: a companion note and then a binary, with `_before_batch_destination_published` rewriting the binary's stage in place with different same-size bytes. Expect `PATH_GUARD_CONTENT` as the cause, both destinations absent, no `BATCH_ROLLBACK_INCOMPLETE` and no publication temporary left; cleanup retains the changed stage and reports `BATCH_CLEANUP_INCOMPLETE`. Record that it fails on unchanged code.
- [x] 1.2 Add a test with `STAT_GENERATION_TRUSTED` set to False: a same-size rewrite of an installed binary destination before a later replacement is detected and the batch rolls back.

## 2. Writer

- [x] 2.1 Hash the bytes copied in `_BatchWorkspace.replace_artifact` and raise `PATH_GUARD_CONTENT` on a digest mismatch before installation; keep the full content check before the legacy rename route.
- [x] 2.2 Record a trusted change witness for stream stages in `create_stream_artifact` and compare it in `_WorkspaceArtifact.recheck` instead of hashing.
- [x] 2.3 Return the installed handle's version from `replace_artifact` and bind installed binary destinations with a `generation` guard where `STAT_GENERATION_TRUSTED`; keep the content policy elsewhere and in post-error binding, and keep the planned digest for rollback.
- [x] 2.4 Remove the duplicate recheck in `_BatchArtifactGuard.capture` and the first of the two post-loop installed-destination rechecks.
- [x] 2.5 Stream the content checks in `unlink_installed` and `bind_installed_after_error`.
- [x] 2.6 Refuse a stage above the classifier's image bound in `vocabulary_gate._stage_bytes` before reading it. Main shipped this refusal independently (#2188); this change keeps main's version.

## 3. Verification and delivery

- [x] 3.1 Run the scoped writer, held-filesystem, binary-producer, adoption and vocabulary-gate tests and compare failure names with the `origin/main` baseline; run Ruff, the privacy gate, the scaffold leak test and `openspec validate --all --strict`.
- [x] 3.2 Measure bytes hashed, wall time and traced peak for a 105 MiB binary in an 8-write batch before and after, with the same probe.
- [x] 3.3 Obtain an independent security-lensed review of the exact head, post its note on the PR, and merge after CI passes.

Delivery: PR #2201 merged as `c3768f5b96e8a9d4cba73ee74eaf61d089e33269` after an independent security-lensed review (two rounds, blocking findings 0 and 0) and green CI.
The release owner coordinates the containing release. No live change was made.
