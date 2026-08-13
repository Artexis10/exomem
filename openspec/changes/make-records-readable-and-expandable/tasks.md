## 1. Lock parser, renderer, and inspection contracts red-first

- [x] 1.1 Test Records-only `record_presentation` v1 parsing/schema, legacy opaque `presentation`, typed child/link columns, references, bounds, normalization, examples, and invalid cases.
- [x] 1.2 Test pure rendering: sections, markers/digests, escaping, observed-value fidelity, bounds, BOM/CRLF/blank/final-newline source fidelity, semantic-body splitting, malformed markers, and policy-independent link literals.
- [x] 1.3 Test inspection: deterministic non-current-first missing/stale/tampered/unrenderable states, counts/truncation/remedies, lifecycle guards, canonical authority, authorize-before-read, and zero read-side mutation.

## 2. Implement managed Markdown-item presentation

- [x] 2.1 Parse/project `record_presentation` through `CollectionManifest`, describe, validate, inspect, and revision checks without changing old manifests.
- [x] 2.2 Implement bounded managed-block rendering/source splicing and automatic Markdown-item append/value-update rendering with authored-byte preservation.
- [x] 2.3 Implement guarded `refresh_presentation`, no-op/invalid refusal, semantic append replay across renderer bytes, audit/idempotency, rollback/crash cuts, and backfill.
- [x] 2.4 Add a generic nested-measurement fixture proving the selected Obsidian summary/table/notes/collapsed-provenance view without inference.

## 3. Fix exact child query projection and expansion

- [x] 3.1 Test Markdown-item zero expansion, explicit selection, missing/ambiguous/open selectors, type/collision failures, expanded/unexpanded undeclared/link-withholding parity, policy-independent stored view, caps, and pagination.
- [x] 3.2 Implement pre-query recursive typed/link projection in every query mode, Markdown-log/table resolution, selected-container omission, exact metadata, collision/total-cap refusal, and cursor identity.
- [x] 3.3 Thread `expand_child` through saved views, governance, Records dispatch, CLI/REST/MCP, schemas, while preserving boolean compatibility.

## 4. Release surfaces and acceptance

- [x] 4.1 Update generic guidance for canonical frontmatter, managed presentation, direct-edit refresh, and explicit child selection without private/domain content.
- [x] 4.2 Regenerate tool schemas, surface contracts, capabilities, and applicable hosted/profile artifacts; prove no unrelated drift.
- [x] 4.3 Extend installed-wheel E2E through opt-in, append/update/read, tamper/diagnose/refresh, safe expanded/unexpanded children, pagination, restart, and audit parity.
- [x] 4.4 Rebase/archive only after the prerequisite nine-action Records change is canonical; run focused/full Records tests, Ruff, Mypy, strict OpenSpec, diff check, wheel, and independent code/security review.
