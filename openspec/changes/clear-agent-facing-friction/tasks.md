## 1. Mutation terminal contract

- [x] 1.1 Write failing pure-logic and invocation tests for compact/full/legacy terminal projection, detail-independent digesting, exact replay after acknowledgement loss, legacy receipt decoding, and unchanged busy/pending/uncertain errors.
- [x] 1.2 Implement the versioned persisted terminal record, generic response-detail adapter wiring, and compact/full/legacy projections without changing pre-commit error semantics.

## 2. Discriminated edit schema

- [x] 2.1 Write failing schema and normalization tests for all seven edit variants, unrelated-field rejection, legacy/new canonical equivalence, JSON-encoded batch compatibility, and one-release deprecation behavior.
- [x] 2.2 Implement Pydantic edit operations, pre-digest normalization, primary MCP/OpenAPI schema projection, and the bounded flat runtime shim over the existing edit leaf.

## 3. Bootstrap capability conformance

- [x] 3.1 Write failing conformance tests that recursively compare tool references in all bootstrap profiles with MCP Tier-2 on/off, REST/OpenAPI, CLI, and hosted exports.
- [x] 3.2 Implement immutable active-surface descriptors and filter every bootstrap route, default, example, catalog, and common-tool reference; distinguish active capability metadata from the canonical MCP fingerprint.

## 4. Action-first audit

- [x] 4.1 Write failing tests with more than the semantic finding cap of grandfathered missing-disposition debt plus current blockers, proving pre-bound prioritization, info/backlog grouping, deterministic samples, exact omission facts, explicit full detail, and lease-free audit routes.
- [x] 4.2 Implement semantic pre-bound ordering and actionable/full audit projections through `audit`, `review_memory`, and `maintain_memory` without changing semantic precommit enforcement.

## 5. Bounded reranking

- [x] 5.1 Write failing candidate-count, validation, cache-key, telemetry, disabled, and soft-fail tests for `rerank_max_candidates`.
- [x] 5.2 Thread the cap through `ask_memory`, `find`, and the hybrid scorer; rerank only the bounded prefix, preserve the fused tail, and document why no hard synchronous latency promise is made.

## 6. Contract, compatibility, and verification

- [x] 6.1 Update generic skill/reference guidance, command docs, REST/OpenAPI coverage, CLI behavior, and migration/deprecation notes while keeping the scaffold generic.
- [x] 6.2 Regenerate and review the MCP schema fixture and packaged discovery fingerprint once; verify the census cache, 600-second implicit replay retention, 60-second single-origin edge budget, and read-only remember preview; then run OpenSpec validation, ruff, focused suites, scaffold leak guard, writer/semantic/reconcile/HA regressions, and the supported full test suite.
- [x] 6.3 Run an independent whole-branch adversarial review and verifier pass, fix all critical/important findings, create logical commits, push the branch, and open a draft PR with fresh-session connector verification called out if still external.
