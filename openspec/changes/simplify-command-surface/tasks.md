## 1. Action Catalog

- [x] 1.1 Add a registry-derived simple action catalog for `ask`, `remember`, `capture`, `review`, `connect`, `adopt`, and `maintain`.
- [x] 1.2 Define canonical routes, default arguments, safety notes, and advanced fallbacks for each simple action.
- [x] 1.3 Add tests proving the action catalog is derived from canonical command metadata and has no orphan routes.

## 2. CLI Aliases

- [x] 2.1 Add `exomem ask` as a thin alias over `find` with compact recall defaults and an explicit deep/context option.
- [x] 2.2 Add write aliases for `remember` and `capture` that route through `note`, `add`, or `preserve` without bypassing validation.
- [x] 2.3 Add read/proposal aliases for `review`, `connect`, `adopt`, and `maintain`, with write-capable behavior explicit.
- [x] 2.4 Preserve canonical registry commands and JSON envelope behavior for aliases.

## 3. Bootstrap And Scaffold Guidance

- [x] 3.1 Update bootstrap to expose simple actions first, with canonical routes and safety posture.
- [x] 3.2 Update `common_tools` or equivalent guidance so generic agents can start with actions and fall through to advanced tools.
- [x] 3.3 Update `_Schema` scaffold guidance and operation references to use user-intent language before tool names.

## 4. Documentation And Generated Artifacts

- [x] 4.1 Update `docs/ai-assistant-guide.md` and `QUICKSTART.md` with the simple action model.
- [x] 4.2 Regenerate `docs/capabilities.md` if command metadata or docs generation changes.
- [x] 4.3 Update MCP schema fixtures only if canonical MCP tool schemas intentionally change.

## 5. Verification

- [x] 5.1 Run focused tests for bootstrap, CLI aliases, command metadata, scaffold leak checks, and MCP schema fidelity.
- [x] 5.2 Run `openspec validate simplify-command-surface`.
- [x] 5.3 Run `git diff --check`.
