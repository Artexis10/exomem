## 1. Contract Tests

- [ ] 1.1 Extend `tests/test_bootstrap.py` to require the bumped bootstrap contract
  version and title-first citation guidance covering path disambiguation, machine-only
  canonical refs, and the explicit-request/debugging exception.
- [ ] 1.2 Add `tests/test_memory_reference_presentation.py` to require equivalent
  title/path/ref guidance in the shipped `SKILL.md` and `references/operations.md`.
- [ ] 1.3 Run the focused tests and confirm they fail because the approved presentation
  guidance is absent, not because of test setup errors.

## 2. Guidance Implementation

- [ ] 2.1 Update `src/exomem/commands.py` to bump the bootstrap contract version and add
  the presentation rule to the existing workflow guidance without adding a response
  field or changing search/read schemas.
- [ ] 2.2 Rewrite the durable-reference section in
  `src/exomem/_scaffold/_Schema/SKILL.md` so titles are visible by default, paths are
  human-readable fallback/disambiguation, and raw refs remain machine-facing unless
  explicitly requested or inspected.
- [ ] 2.3 Apply the equivalent contract to the stable-identity section in
  `src/exomem/_scaffold/_Schema/references/operations.md`, including the
  custom-scheme Markdown-link warning.
- [ ] 2.4 Re-run the focused tests and confirm they pass.

## 3. Verification and Delivery

- [ ] 3.1 Run the scaffold leak guard and Ruff over the changed Python/test files.
- [ ] 3.2 Run the full model-disabled test suite and `openspec validate
  human-readable-memory-citations`.
- [ ] 3.3 Review the final diff against every OpenSpec scenario, mark completed tasks,
  and commit the implementation on the isolated feature branch.
- [ ] 3.4 Obtain an independent code/spec review and resolve all substantive findings.
