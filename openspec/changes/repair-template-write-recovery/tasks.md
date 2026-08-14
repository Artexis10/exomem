## 1. Frontmatter-less editor

- [x] 1.1 Add red leaf and public-command tests for validate/commit of `replace_string`, `replace_body`, `batch_replace`, and `edit_section` on frontmatter-less Markdown, including exact hashes and no synthesized YAML.
- [x] 1.2 Add red tests proving tag/frontmatter-dependent operations refuse without a bogus `missing: ['path']` suffix.
- [x] 1.3 Implement operation-owned frontmatter eligibility and one shared exact renderer for `edit` and `multi_edit`; rerun focused editor and semantic-handshake tests.

## 2. Tier-two overwrite token symmetry

- [x] 2.1 Add a red MCP-shaped create-plus-overwrite validate/commit test that copies `draft_token` by name and asserts byte-identical `transition_token` compatibility.
- [x] 2.2 Add the overwrite-only response alias, update schema guidance/fixtures, and rerun semantic lifecycle plus command-surface fidelity tests.

## 3. Graph failure and explicit recovery

- [x] 3.1 Add red coordinator tests proving the original builder exception is logged/chained and terminal remediation differs for recoverable versus unavailable epoch state.
- [x] 3.2 Implement diagnostic preservation and state-aware content-free remediation without changing the canonical committed terminal.
- [x] 3.3 Add red dry-run, successful reset, partial-rollback, open-reader, reparse, canonical-hash preservation, and rebuild-failure quarantine tests for `rebuild_graph=true`.
- [ ] 3.4 Implement bounded derived graph quarantine/reset under the existing mutation boundary and wire the default-false parameter through reconcile, maintain-memory, CLI, REST, MCP, and generated schema fixtures.
- [x] 3.5 Rerun graph availability, idempotency handoff, writer lease, reconcile, watcher, mutation-terminal, and command-surface tests.

## 4. Windows semantic-sidecar audit

- [ ] 4.1 Add red platform-seam tests for truthful unsupported classification and native Windows tests for a healthy sidecar, reparse refusal, identity change, handle closure, and later replacement.
- [ ] 4.2 Implement retained no-follow Windows sidecar bindings and distinct absent/readable/unreadable/schema-unreadable/unsupported census outcomes.
- [ ] 4.3 Rerun semantic-isolation census, exact-row purge, Records reconcile, Windows path/alias, and scaffold leak tests.

## 5. Verification and delivery

- [ ] 5.1 Validate the OpenSpec change strictly; run Ruff, targeted Mypy, `git diff --check`, focused suites, and the embeddings-disabled project suite with clean temporary/runtime roots.
- [ ] 5.2 Build the wheel and run installed-wheel product reproductions for frontmatter-less editing, overwrite token replay, graph failure/recovery, and Windows census; retain exact command evidence.
- [ ] 5.3 Obtain an independent code/security review and independent end-to-end verifier pass, fix confirmed findings, and rerun invalidated gates.
- [ ] 5.4 Commit only intended scope, integrate the current remote default branch safely in this worktree, push, and open a ready Conventional Commit pull request with rationale and verification evidence.
