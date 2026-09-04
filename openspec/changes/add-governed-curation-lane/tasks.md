## 1. Plan model and canonical store

- [ ] 1.1 Add red pure-logic tests for canonical plan JSON, plan/run ids, strict step schemas, binding manifests, fingerprints, size caps, collisions, and unknown-field refusal.
- [ ] 1.2 Implement the curation plan dataclasses/validators and closed v1 step vocabulary without any leaf dispatch.
- [ ] 1.3 Add red store tests for create-only forward/compensation plans, state reconstruction from receipts, protected no-follow paths, and corrupt or competing artifacts.
- [ ] 1.4 Implement the governed `_Governance/curation/runs` store for immutable plans, mutable projection state, create-only receipts, and evidence lookup.
- [ ] 1.5 Add path-policy tests and enforcement that refuse Planning, Records, workflow-contract, `_Schema`, `_Governance`, `_Adoption`, trash internals, and other protected targets outside the run store.

## 2. Deterministic work items and proposals

- [ ] 2.1 Add red tests for bounded `work-item` assembly from explicit refs/paths, exact current hashes and registry ids, truncation disclosure, Unicode preservation, and no model calls.
- [ ] 2.2 Implement deterministic work-item assembly and publish the closed per-kind input contracts.
- [ ] 2.3 Add red proposal tests for stale agent bindings, ordered validate-only preparation, expected absence, registry drift, semantic/relation tokens, compensation pre-images, and immutable plan preview.
- [ ] 2.4 Implement forward proposal validation by adapting the existing remember/entity/relation/edit/replace/move/delete/recover validation paths without writing knowledge content.
- [ ] 2.5 Implement read-only preview/status projections with exact actions, binding health, blockers, phase, receipts, and next permitted action.

## 3. Atomic step witnesses

- [ ] 3.1 Add a red shared contract test proving a curation operation witness commits in the same batch as its content effect and never appears on a refused or rolled-back mutation.
- [ ] 3.2 Add the private commit-witness seam to semantic create/edit/replace internals while keeping public command schemas and ordinary results unchanged.
- [ ] 3.3 Add witness adapters and parity tests for `create-note`, `edit`, and `supersede` against their ordinary governed leaves.
- [ ] 3.4 Add witness adapters and parity tests for `create-entity` and `accept-relation`, including relation fingerprint/hash revalidation.
- [ ] 3.5 Add witness adapters and parity tests for governed `move`, `delete`, and `recover`, including canonical delete confirmation and exact trash identity.
- [ ] 3.6 Reject any enabled step kind whose leaf cannot prove one atomic effect+witness commit.

## 4. Forward execution and recovery

- [ ] 4.1 Add red tests for exact-plan approval, bounded rationale, live revalidation under the mutation boundary, one-step execution, and refusal to resume another plan.
- [ ] 4.2 Implement apply/resume with durable approval, deterministic operation ids, prepared state, single-step dispatch, terminal receipts, and phase derivation.
- [ ] 4.3 Add deterministic fault barriers after prepared-state commit, leaf+witness commit, and terminal-receipt commit.
- [ ] 4.4 Add crash/restart tests at every barrier proving read-only status never repairs, exact resume produces zero-or-one effect and recovered-committed receipts, replay selects the correct next step, and invalid evidence blocks.
- [ ] 4.5 Add red tests and implementation for clean refusal, retryable pre-commit failure, stale-plan failure, partial runs, failed runs, completed replay, and state reconstruction after projection loss.
- [ ] 4.6 Integrate curation step outcomes with the existing mutation terminal, idempotency, graph settlement, due-state batch carrier, and compact/full response projections.

## 5. Compensation

- [ ] 5.1 Add red derivation tests for reverse ordering and the exact compensation mapping for creation, deletion, move, edit, accepted relation, supersession, and recovery receipts.
- [ ] 5.2 Implement create-only compensation plans from committed witnesses/receipts and sealed pre-step material, with fresh live bindings and forward-plan linkage.
- [ ] 5.3 Add red approval/execution tests proving compensation has a distinct fingerprint/rationale, runs one step per request, and preserves all forward evidence.
- [ ] 5.4 Implement compensation apply/resume through the same witness and receipt protocol.
- [ ] 5.5 Add crash tests at every compensation barrier and drift/collision tests proving later work is never overwritten to force a reversal.

## 6. Product command and standalone parity

- [ ] 6.1 Add red registry/schema tests for `maintain_memory(mode="curation")`, the eight finite actions, curation-only arguments, and conservative unknown-action handling.
- [ ] 6.2 Wire the curation actions into `op_maintain_memory` and `invocation_is_read_only`, keeping all non-curation maintenance behavior unchanged.
- [ ] 6.3 Add MCP, REST, OpenAPI, and CLI parity tests over the shared leaf, including application errors and response-detail projection.
- [ ] 6.4 Add standalone restart tests proving the governed run artifacts, not a Hosted or machine-local service, are sufficient to inspect, recover, resume, and compensate.
- [ ] 6.5 Regenerate the golden MCP schema and generated capability documentation with an exact intentional-diff assertion for `maintain_memory` only.

## 7. Hosted admission and release artifacts

- [ ] 7.1 Add the generic synthetic curation contribution input at `tests/fixtures/hosted_v5_contributions/governed_curation.json`; it is sibling-owned input outside the candidate tree, not a v5 release file.
- [ ] 7.2 In the single v5 owner lane, add red profile-version tests proving v1-v4 keep their pinned legacy schema and refuse curation-only arguments while v5 discovers curation and routes it through the generic registry boundary.
- [ ] 7.3 In that owner lane, make the request-bound `curation` exception conditional on the active profile's pinned schema; preserve pre-dispatch refusal for v1-v4 curation and for fix, reconcile, backfill, and unknown modes everywhere.
- [ ] 7.4 Add v5 tenant-isolation, entitlement, path-confinement, idempotency, process-restart recovery, and edge-duration tests for one-step execution without a candidate-specific executor.
- [ ] 7.5 Require the v5 owner to canonicalise and freeze the curation contribution into the candidate-owned combined fixture and include it in compatibility, package, lock, archive, promotion, and clean-client evidence; fixture drift MUST invalidate package verification.
- [ ] 7.6 Prove one complete v1-v4 immutability manifest covering source bytes, generated bytes, locks, archives, definitions, fixtures, promotion records, resolved command schemas, and actual-wire identities.

## 8. Agent contract and no-nudge integration

- [ ] 8.1 Add red scaffold tests that map work-item/propose/preview to `structural_suggestions` and apply/resume/compensation to confirm-required `restructure_execution`.
- [ ] 8.2 Update the generic `exomem-curate` workflow skill and core operation references to preview once, obtain explicit confirmation once per immutable plan, resume automatically until terminal, and stop on partial/blocked state.
- [ ] 8.3 Add hookless bootstrap/custom-instruction carrier assertions without hardcoding an English-only workflow or weakening explicit-request availability when structural suggestions are off.
- [ ] 8.4 Pass scaffold privacy/leak checks and verify no Planning/Records/OpenSpec workflow automation was added.

## 9. Completion and OpenSpec closure

- [ ] 9.1 Run the affected curation, semantic writer, file governance, command-surface, mutation-terminal, Hosted, workflow-skill, and artifact-freshness suites with explicit scope.
- [ ] 9.2 Run `ruff check`, the full lean pytest corpus, strict OpenSpec validation, lock/diff checks, privacy scan, and all release artifact freshness gates; record unrelated environmental failures separately from attributable failures.
- [ ] 9.3 Exercise one multilingual forward plan, crash recovery, partial run, exact replay, and compensation end to end on both standalone and Hosted test harnesses.
- [ ] 9.4 After the shared v5 delivery and all four changes' merge/review evidence are complete, participate in the owner-controlled sync/archive sequence: `add-governed-curation-lane`, then `complete-recurring-entity-lifecycle`, then `preserve-adopted-generated-artifacts`, then `capture-durable-personal-baselines`, with strict validation before and after every archive.
