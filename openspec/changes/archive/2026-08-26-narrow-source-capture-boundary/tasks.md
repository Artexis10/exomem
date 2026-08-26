## 1. Reproduce

- [x] 1.1 Write an ordering probe that records every staging fetch and every entry into `LeaseManager.mutation_guard`, and assert a one-file `capture_source` fetches before the lock is taken. Confirm it fails on `origin/main` with the lock recorded first.
- [x] 1.2 Assert the whole batch stages before the first commit, not only the first file.
- [x] 1.3 Assert a text capture still records the wide `capture_source` boundary. Confirm this one passes before the fix.

## 2. Fix

- [x] 2.1 Add `narrow_source_artifact_commit` to the boundary selection in `writer_lease.invoke_command`, gated on `command.name == "capture_source"`, a truthy `kwargs["files"]`, and the absence of `EXOMEM_WIDE_MUTATION_BOUNDARY`.
- [x] 2.2 Record in a comment why the command cannot join `_NARROW_BOUNDARY_COMMANDS`.
- [x] 2.3 Assert `EXOMEM_WIDE_MUTATION_BOUNDARY` restores the wide boundary for the file lane.

## 3. Verify

- [x] 3.1 Run `tests/test_attachment_source_ingestion.py`.
- [x] 3.2 Run `tests/test_client_artifacts.py`, `tests/test_shorten_critical_section.py`, `tests/test_writer_lease.py`, `tests/test_tier2.py`.
- [x] 3.3 Confirm `tests/fixtures/mcp_tool_schemas.json` and `src/exomem/tool_surface_contract.json` are unchanged.
- [x] 3.4 Run `openspec validate narrow-source-capture-boundary --strict` and `openspec validate --specs --strict`.
- [x] 3.5 Run lint.

## 4. Closure

- [x] 4.1 Once merged and therefore demonstrably shipped, sync the delta into `openspec/specs/` and archive with `openspec archive`, re-running `openspec validate --all --strict` before and after. Left open deliberately: archiving before the merge would claim a shipped state that does not exist, and the archive-discipline check treats a fully-ticked active change as debt.
