# Tasks: add-cross-domain-bridges

## 1. Strict bridge contract

- [x] 1.1 Red focused tests for all-or-none compiled bridge metadata; strict
      `kind: release` parsing; exact path/ref/hash/audience/dependency binding;
      malformed, duplicate, copied, alias, traversal, and ambiguous identities.
- [x] 1.2 Implement release grants outside standing/session/lattice evaluation;
      enforce bridge ordinary ceiling/purpose narrowing and `RELEASE_UNAPPROVED` /
      path-free `RELEASE_STALE` admission outcomes.
- [x] 1.3 Implement deterministic, audience-scoped dependency signatures over
      membership, constraints, relevant rules/typed options, and standing grants;
      stale relevant changes and preserve unrelated-policy releases.

## 2. Authoring, approval, and receipts

- [x] 2.1 Red focused tests for normal remember/replacement validate-review-commit:
      all-or-none fields, stable-ref normalization, draft hash/token binding, and
      unreleased committed bridge drafts.
- [x] 2.2 Wire bridge authoring through normal review flow; implement separate
      owner-reviewed `govern_memory propose -> commit` approval/reapproval of the
      exact bridge, including durable receipt and causation/recovery evidence.

## 3. Admission and egress

- [x] 3.1 Red tests for byte-compatible empty-policy no-parse/no-state behavior,
      active unrelated-policy ordinary notes, immutable snapshots, hot cache,
      same-size/mtime edits, and retrieval/projection swaps.
- [x] 3.2 Centralize post-cache exact-byte admission for direct/immutable reads,
      decide/explain/simulate, page/unit/mixed search, graph, pack, review
      context, and terminal filtering.
- [x] 3.3 Red and implement recursive stripping against approval-resolved targets
      across frontmatter, raw content, Relations, history, links, graph,
      relation/supersession/parent/matched units, and title/path/ref aliases,
      including dependencies absent from the result pool.

## 4. Constraints and lifecycle

- [x] 4.1 Red and implement deterministic scope constraint strings: L2 only when
      ordinary policy permits, one distinct string, ambiguity below L2,
      provenance-free output, and separate legacy option fallback.
- [x] 4.2 Red and implement default, read-only `bridge_review` findings: generic
      causes, stable signal versions, no confidential provenance, due-not-expiry,
      triage nonapproval, dismissal fact-change resurfacing, audience isolation,
      and exact reapproval clearing staleness.

## 5. Contract regeneration and gates

- [x] 5.1 Regenerate and verify schema, connector pending digest, packaged plugin
      tool surface, bootstrap/capability contracts when public signatures change.
- [x] 5.2 Record RED/GREEN and receipt/recovery evidence; run focused suites:
      `tests/test_governance_bridges.py`, `test_governance_egress.py`,
      `test_govern_memory_tool.py`, `test_attention.py`, `test_review_state.py`,
      `test_bootstrap.py`, and `test_tool_surface_contract.py`.
- [x] 5.3 Run the latency gate, Ruff on changed Python files, strict OpenSpec
      validation, `git diff --check`, and `uv build` with the configured sandbox
      cache/state environment.
