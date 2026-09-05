## 1. Protocol and evidence contract

- [ ] 1.1 Add failing pure-logic tests for family descriptors, allowed outcomes, fingerprint/version binding, unknown-family refusal and non-inheritance of future actions; verify the tests fail for the missing behaviours before implementation.
- [ ] 1.2 Implement the family/work-item protocol and typed decision validation in focused vocabulary modules; verify the tests from 1.1, including a synthetic future-family adapter that cannot load code or grant itself authority.
- [ ] 1.3 Implement registry-aware evidence adapters over the shipped relation resolver and entity-type registry without changing their persistence semantics; verify exact aliases, distinct nearby meanings, unavailable recurrence and bounded continuation in scoped tests.
- [ ] 1.4 Implement the decision state machine and receipt-bound completion in review state; verify stale decisions refuse, proposal is not applied, committed receipts reconcile once, and unchanged evidence retains its disposition across restart.

## 2. Ordinary-work guidance under existing v1 permissions

- [ ] 2.1 Add failing tests for a resolved generic pair/edge supported by two independent origins, copy-equivalent origin negatives, explicit agent meaning questions, single keyword negatives and unavailable independence evidence; verify the current unknown-label-only advisory cannot satisfy the positive case and the runtime never labels a supplier/equivalence meaning.
- [ ] 2.2 Connect current-write context and bounded indexed origin/pair evidence to vocabulary work items in mutation/review projections; verify one advisory of at most 1 KiB per write, four items per default review page, explicit continuation, and zero full-corpus scans or model calls on the write path.
- [ ] 2.3 Implement fingerprinted notification deduplication, deferral, family quiet and state-based resolution without moving original evidence; verify restart persistence, explicit access to quiet work, no re-notification from unrelated registry edits, and that linked non-quietable integrity findings remain visible until state repair.
- [ ] 2.4 Add soft-failure and recovery tests before implementing optional guidance recovery; verify committed terminals keep their receipt/replay identity, unavailable is not empty, and bounded projection recovery reconstructs missed work without repeating the content mutation.
- [ ] 2.5 Expose review/context/typed-decision variants through canonical command definitions and supported MCP/REST/CLI adapters; verify cross-surface schema fidelity, capability discovery and the same leaf results without a generic execution payload.
- [ ] 2.6 Update bootstrap and the generic skill scaffold with pre-write consideration, review disposition, truthful generic/no-edge examples and v1 permission limits; verify bootstrap/scaffold contract tests, hookless consumption and the scaffold privacy gate.

## 3. Entity and curation integration

- [ ] 3.1 Reconcile the overlapping `complete-recurring-entity-lifecycle` and `add-governed-curation-lane` artifacts with their owners before integration: retain all v1 confirmation scenarios and name this change as the sole v2 authority dependency; verify strict OpenSpec validation and independent review of the resulting requirements.
- [ ] 3.2 Connect the entity-lifecycle provider's ordinary-text promotion, hydration and ambiguity evidence to the common work-item layer once its own acceptance passes; verify recurring identities, useful existing-entity enrichment, alias matches, ambiguous identities and incidental-name negatives without duplicating its detector.
- [ ] 3.3 Bind multi-step vocabulary proposals to governed curation previews and individual canonical receipts once that lane's acceptance passes; verify registration followed by entity/edge application, interrupted resume, stale targets and truthful partial completion.
- [ ] 3.4 Exercise custom entity/relationship types through registration, authored use, exact and parent-family retrieval/traversal, deprecation history and warming-to-current graph publication; verify real downstream behaviour rather than registry counts alone.

## 4. Versioned additive authority foundation

- [ ] 4.1 Add failing authorization tests for opt-in activation, default no grants, separate entity/type/edge actions, vault-wide type/entity creation grants, exact approval for narrower new entities and resolved project edge scope; verify v1 is unchanged, new entities cannot assert project membership and an older client cannot downgrade an activated vault.
- [ ] 4.2 Implement activation/grant records under external authority custody, reusing existing principal/session/secret-handling contracts; verify restart persistence, bounded expiry, generation validation and cross-principal/cross-vault isolation.
- [ ] 4.3 Implement pending approval request/status variants and a trusted user-control approval ceremony distinct from ordinary agent credentials; verify agent self-approval, forged confirmation fields and retrieved permission text cannot mint approval, while an authenticated user approves the displayed exact effects. Inspect the real control surface with Chrome DevTools if rendered UI changes.
- [ ] 4.4 Implement one-shot exact-action approval and scoped grant evaluation over canonical payloads/targets; verify single operation-identity reservation, proven pre-commit retry, uncertain-outcome recovery, refusal under a new identity, out-of-scope endpoints, project-label laundering, global registry effects, changed reviewed hashes and unsupported future actions.

## 5. Enforcement and recovery gates

- [ ] 5.1 Add failing complete-before/after effect-classification tests for typed writers, generic file creation/replacement, imports and registry saves; verify every route enters the same authority gate in v2, a permitted type addition mixed with an unauthorized existing-definition change refuses atomically, and entity creation cannot smuggle connections or a new project-key registration through an entity-only grant.
- [ ] 5.2 Integrate effect classification and live authority validation into canonical mutation leaves under their commit boundary; verify permission cannot be bypassed through a legacy adapter, generic payload or cached preview and all existing validation/write-scope/lease gates remain intact.
- [ ] 5.3 Serialize revocation against commit and bind non-secret authority evidence to canonical receipts; verify revoke-before-commit refusal, committed-before-revoke history, atomic one-shot spend/receipt linkage with crash recovery, replay without reapplication, current receipt disclosure and expired-authority refusal for remaining curation steps.
- [ ] 5.4 Implement runtime admission and downgrade fences for activated authority state; verify supported deploy/rollback routes cannot serve weaker structural writes, unreadable authority refuses writes but permits authorized reads, and no migration silently grants authority.
- [ ] 5.5 Obtain independent security review of the trusted approval channel, complete writer-route inventory, scope checks, revoke/commit race and downgrade reproduction; verify each blocking finding is resolved with rerunnable evidence before exposing v2 activation.

## 6. Ordinary-agent acceptance and delivery

- [ ] 6.1 Create a bounded ordinary-domain acceptance cohort with no vocabulary-mechanism hints, including useful custom entity/relation types, reused types, hydration, generic/no-edge and incidental-name negatives; verify expected outcomes are supported by the supplied evidence rather than quotas.
- [ ] 6.2 Run that cohort through an active agent using the served contract and preserve delivered items, decisions, canonical receipts and retrieval/traversal outputs; verify each positive and negative case independently and distinguish absent advice, ignored advice and failed application.
- [ ] 6.3 Compare ordinary-path latency, memory and projection-query work against the pre-change baseline on the same fixtures; verify declared response bounds, no per-write corpus scan/model load and no retained-knowledge or session-count limits introduced by this work.
- [ ] 6.4 Run affected suites during development, then the full lean suite, required integration/capability checks, lint and public-artifact/privacy validation at the completion boundary; verify actual outputs and obtain independent end-to-end verification of the claimed tranche.
- [ ] 6.5 Publish the implementation PRs with conventional titles and verification evidence; after authorized merges, verify default-branch state and remove only clean, pushed task worktrees with no live processes.
- [ ] 6.6 After all non-optional integrations and acceptance tasks are evidenced as shipped, synchronize these deltas without discarding later scenarios and archive through OpenSpec; verify `openspec validate --all --strict` before and after archive. A planning PR alone does not complete or archive this change.
