Retirement scope (2026-10-11): Retirement supersedes Hosted v5 admission and package gates. Shared curation, remote maintenance refusal, portable evidence, and pending closure remain in scope.

## 1. Plan model and canonical store

- [x] 1.1 Add red pure-logic tests for canonical plan JSON, plan/run ids, strict step schemas, binding manifests, fingerprints, size caps, collisions, and unknown-field refusal.
- [x] 1.2 Implement the curation plan dataclasses/validators and closed v1 step vocabulary without any leaf dispatch.
- [x] 1.3 Add red store tests for create-only forward/compensation plans, exact-path relocation candidate/authorization records, state reconstruction from governed evidence, protected no-follow paths, and corrupt or competing artifacts.
- [x] 1.4 Implement the governed `_Governance/curation/runs` store for immutable plans, exact transition records, mutable projection state, create-only receipts, and evidence lookup.
- [x] 1.5 Add path-policy tests and enforcement that refuse Planning, Records, workflow-contract, `_Schema`, `_Governance`, `_Adoption`, trash internals, and other protected targets outside the run store.

## 2. Deterministic work items and proposals

- [x] 2.1 Add red tests for bounded `work-item` assembly from explicit refs/paths, exact current hashes and registry ids, truncation disclosure, Unicode preservation, and no model calls.
- [x] 2.2 Implement deterministic work-item assembly and publish the closed per-kind input contracts.
- [x] 2.3 Add red proposal tests for stale agent bindings, ordered validate-only preparation, expected absence, registry drift, semantic/relation tokens, compensation pre-images, and immutable plan preview.
- [x] 2.4 Implement forward proposal validation by adapting the existing remember/entity/relation/edit/replace/move/delete/recover validation paths without writing knowledge content.
- [x] 2.5 Implement read-only preview/status projections with exact actions, binding health, blockers, phase, receipts, and next permitted action.

## 3. Exact step commit evidence

- [x] 3.1 Add red shared contract tests proving content-write witnesses commit in the same batch as their effects, while move/delete/recover have ordered durable operation-bound candidate and authorization records before rename and reconstructible final evidence at every cut.
- [x] 3.2 Add the private commit-witness seam to semantic create/edit/replace internals while keeping public command schemas and ordinary results unchanged.
- [x] 3.3 Add witness adapters and parity tests for `create-note`, `edit`, and `supersede` against their ordinary governed leaves.
- [x] 3.4 Add witness adapters and parity tests for `create-entity` and `accept-relation`, including relation fingerprint/hash revalidation.
- [x] 3.5 Prove governed `move`, `delete`, and `recover` fail closed as `CURATION_RENAME_HISTORY_UNPROVABLE` at proposal, preview, and apply, before any transition record is published and before any rename is attempted, leaving no rename, rollback, trash write, candidate, authorization, witness, receipt, or governed run store. Relocation protocol: add-curation-relocation-protocol.
- [x] 3.6 Reject any enabled step kind whose effect is neither same-batch witnessed nor exactly reconstructible through the prepared-relocation protocol.

## 4. Forward execution and recovery

- [x] 4.1 Add red tests for exact-plan approval, bounded rationale, live revalidation under the mutation boundary, one-step execution, and refusal to resume another plan.
- [x] 4.2 Implement apply/resume with durable approval, deterministic operation ids, prepared state, single-step dispatch, terminal receipts, and phase derivation.
- [x] 4.3 Add deterministic fault barriers after prepared-state commit, content leaf+witness commit, and terminal-receipt commit. Relocation protocol: add-curation-relocation-protocol.
- [x] 4.4 Add crash/restart tests at every content barrier proving read-only status never repairs, exact resume produces zero-or-one durable semantic effect and recovered-committed receipts, generation-history failures return `CURATION_RENAME_HISTORY_UNPROVABLE`, replay selects the correct next step, and invalid or ambiguous evidence blocks. Relocation protocol: add-curation-relocation-protocol.
- [x] 4.5 Add red tests and implementation for clean refusal, retryable pre-commit failure, stale-plan failure, partial runs, failed runs, completed replay, and state reconstruction after projection loss.
- [x] 4.6 Integrate curation step outcomes with the existing mutation terminal, idempotency, graph settlement, due-state batch carrier, and compact/full response projections.

## 5. Compensation

- [x] 5.1 Add red derivation tests for reverse ordering and the exact compensation mapping for creation, deletion, move, edit, accepted relation, supersession, and recovery receipts.
- [x] 5.2 Implement create-only compensation plans from committed witnesses/receipts and sealed pre-step material, with fresh live bindings and forward-plan linkage.
- [x] 5.3 Add red approval/execution tests proving compensation has a distinct fingerprint/rationale, runs one step per request, and preserves all forward evidence.
- [x] 5.4 Implement compensation apply/resume through the same witness and receipt protocol.
- [x] 5.5 Add crash tests at every compensation barrier and drift/collision tests proving later work is never overwritten to force a reversal.

## 6. Product command and standalone parity

- [x] 6.1 Add red registry/schema tests for `maintain_memory(mode="curation")`, the eight finite actions, curation-only arguments, and conservative unknown-action handling.
- [x] 6.2 Wire the curation actions into `op_maintain_memory` and `invocation_is_read_only`, keeping all non-curation maintenance behavior unchanged.
- [x] 6.3 Add MCP, REST, OpenAPI, and CLI parity tests over the shared leaf, including application errors and response-detail projection.
- [x] 6.4 Add standalone restart tests proving the governed run artifacts, not a Hosted or machine-local service, are sufficient to inspect, recover, resume, and compensate.
- [x] 6.5 Regenerate the golden MCP schema and generated capability documentation with an exact intentional-diff assertion for `maintain_memory` only.

## 8. Agent contract and no-nudge integration

- [x] 8.1 Add red scaffold tests that map work-item/propose/preview to `structural_suggestions` and apply/resume/compensation to confirm-required `restructure_execution`.
- [x] 8.2 Update the generic `exomem-curate` workflow skill and core operation references to preview once, obtain explicit confirmation once per immutable plan, resume automatically until terminal, and stop on partial/blocked state.
- [x] 8.3 Add hookless bootstrap/custom-instruction carrier assertions without hardcoding an English-only workflow or weakening explicit-request availability when structural suggestions are off.
- [x] 8.4 Pass scaffold privacy/leak checks and verify no Planning/Records/OpenSpec workflow automation was added.

## 9. Completion and OpenSpec closure

- [x] 9.1 Run the affected curation, semantic writer, file governance, command-surface, mutation-terminal, Hosted, workflow-skill, and artifact-freshness suites with explicit scope. Evidence: tests/test_curation_*.py, semantic writer, file governance, command-surface, mutation-terminal, hosted, workflow-skill and artifact-freshness suites green on the delivered tip.
- [x] 9.2 Run `ruff check`, the full lean pytest corpus, strict OpenSpec validation, lock/diff checks, privacy scan, and all release artifact freshness gates; record unrelated environmental failures separately from attributable failures. Evidence: ruff clean; full lean suite on the delivered tip; strict validate 182/182; lock/diff checks and privacy scan clean.
- [x] 9.3 Exercise one multilingual forward plan, crash recovery, partial run, exact replay, and compensation end to end on both standalone and Hosted test harnesses. Evidence: standalone tests/test_curation_plan.py and tests/test_curation_execution.py (Finnish forward plans, terminal-receipt crash replay), tests/test_curation_compensation.py; Hosted tests/test_hosted_private_routes.py::test_v5_curation_* (isolation, one step per request, replay, process restart, fast-ack seam).
- [ ] 9.4 After current client acceptance and all four changes' merge/review evidence are complete, participate in the owner-controlled sync/archive sequence: `add-governed-curation-lane`, then `complete-recurring-entity-lifecycle`, then `preserve-adopted-generated-artifacts`, then `capture-durable-personal-baselines`, with strict validation before and after every archive.

## 10. Intra-plan dependencies and late approval

- [x] 10.1 Render each proposed and compensation step's links to pages the plan's earlier steps create, and refuse with `CURATION_STEP_CONFLICT` a step bound to a path an earlier step writes, a link to a page an earlier step supersedes, and a relation disposition on a step that links to a page an earlier step creates. Evidence: `curation._prepare_manifest`, `find.planned_pages` (PR #2246).
- [x] 10.2 Mint an edit step's transition at execution, never from the plan, and refuse before any write a planned page that differs from the sealed postcondition apart from the reviewed `updated` instant. Evidence: `curation._dispatch_step`, `curation._require_reviewed_edit`, `edit.reviewed_render` (PR #2246).
- [x] 10.3 Prove both end to end: a two-page split linked by path and by title applies; a foreign link target still refuses; an edit approved days later applies through each edit writer and with a copied token; an edit whose render changed refuses with nothing written; each conflicting plan is refused at proposal. Evidence: tests/test_curation_plan_dependencies.py, 13 cases, with the existing curation suites green on the delivered tip (PR #2246).
- [x] 10.4 Refuse at proposal a create or supersede step on a fully governed page whose relation disposition its leaf validation does not require (`RELATION_REVIEW_NOT_APPLICABLE`), since the leaf refuses it at commit; a review on a structural page still applies. Evidence: `curation._require_committable`, tests/test_curation_plan_dependencies.py (PR #2246).
- [x] 10.5 Refuse at proposal with `SEMANTIC_CONTRACT_BLOCKED` a create, supersede or edit step whose leaf validation reports a blocking finding that the step does not answer; its own review or a link to a page an earlier step creates answers a relation-disposition finding. Evidence: `curation._refuse_blocked`, tests/test_curation_plan_dependencies.py.
