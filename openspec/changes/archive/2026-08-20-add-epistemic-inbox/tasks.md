## 1. Canonical Markdown Relations

- [x] 1.1 Add a shared parser and governed vocabulary for canonical `## Relations` bullets and semantic-block relation validation.
- [x] 1.2 Index canonical note relations as typed graph edges without redundant generic edges, preserving legacy relation-line compatibility.
- [x] 1.3 Extend compiled-write feedback and the portable agent scaffold with typed note/block relation counts and canonical authoring guidance.

## 2. Relation Debt Review

- [x] 2.1 Add deterministic `relation_debt` audit detection with lifecycle/access exclusions and content-derived signal versions.
- [x] 2.2 Compose relation debt into attention ranking, filters, summaries, and review guidance.
- [x] 2.3 Add focused parser, graph, audit, attention, and write-feedback tests.

## 3. Stable Epistemic Inbox

- [x] 3.1 Add deterministic review IDs, `exomem://review/<id>` references, target/related refs, and signal fingerprints.
- [x] 3.2 Implement versioned portable review state with atomic dismiss, snooze, reopen, expiry, and fingerprint-resurfacing behavior.
- [x] 3.3 Add state-aware attention filtering and item lookup while preserving base scores and existing response compatibility.

## 4. Product Surfaces

- [x] 4.1 Add the explicit write-capable `triage_memory` registry command and extend read-only `review_memory` across MCP, REST, CLI, and OpenAPI.
- [x] 4.2 Render a concise human `exomem review` inbox and add dismiss/snooze/reopen aliases while preserving `--json`.
- [x] 4.3 Regenerate schema/capability fixtures and add registry, REST, CLI, and consolidated-tool tests.

## 5. Documentation And Verification

- [x] 5.1 Document canonical relation syntax, the review lifecycle, state semantics, and incremental legacy-vault repair.
- [x] 5.2 Run focused tests, the default suite, Ruff, scaffold leak checks, and strict OpenSpec validation. (The sandbox's existing FastMCP/TestClient portal stalls at the first transport request; 1,696 non-transport tests and all focused/schema gates pass, with transport coverage left to CI.)
- [x] 5.3 Smoke-test the human and JSON CLI paths against a fixture vault and verify no model or background worker loads.
