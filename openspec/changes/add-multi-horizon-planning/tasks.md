## 1. Acceptance Fixtures And Red Contracts

- [x] 1.1 Add a software Planning fixture with one area, outcome, initiative, bug/feature work items, week/quarter/year/multi-year horizons, a thin OpenSpec/repository pointer, direct-edit variants, duplicate titles/IDs, and invalid state/hierarchy/cycle cases.
- [x] 1.2 Add a materially different non-software multi-year fixture with its own domain fields and a bounded Records collection/saved-view evidence pointer; do not use another software or workout backlog.
- [x] 1.3 Freeze current Records manifest, `record_id`, `record_audit`, item marker, activity-event, `exomem://record/...`, query, mutation, inspection, governance, and public-command response behavior with focused compatibility tests before extracting shared mechanics.
- [x] 1.4 Write failing Planning manifest/profile/storage/path tests for exact `Knowledge Base/Planning/**` placement, Markdown-item-only product storage, supported/unknown versions, duplicate collection identity, template independence, and Records/Planning cross-profile refusal.
- [x] 1.5 Write failing Planning core-schema/default/lifecycle/horizon/date tests, including area field exemptions, duplicate titles, undeclared direct-edit fields, no inferred rebucketing, and explicit archival that preserves status.
- [x] 1.6 Write failing identity, `exomem://plan/...`, hierarchy, area, cycle, archived-parent/target, exact evidence/execution shape, and hidden-target parity tests.

## 2. Profile-Neutral Structured Collection Core

Primary ownership: `src/exomem/structured_collections.py`, `src/exomem/record_formats.py`, `src/exomem/records.py`, `src/exomem/record_governance.py`, new `src/exomem/collection_profiles.py`, new `src/exomem/planning.py`, and new `src/exomem/planning_governance.py`. Limit extraction to Markdown-items identity, parsing/writing, bounded query, audit, guarded mutation, and governance seams used by both profiles; keep public Records facades and serialized shapes exact and do not wholesale-rename modules.

- [x] 2.1 Introduce the immutable profile contract for placement, ID property, reference namespace, manifest/item audit names, reserved system fields, operation labels, semantic validation, and response projection; register Records and Planning without domain conditionals in the shared engine.
- [x] 2.2 Generalize collection item identity/reference parsing so Planning uses `plan_id` and `exomem://plan/...` while every existing Records reference and natural-key compatibility path remains exact.
- [x] 2.3 Generalize Markdown-item reserved frontmatter and marker parsing/writing behind the profile contract, preserving existing Records bytes and accepting ordinary Planning item files.
- [x] 2.4 Generalize manifest audit-head parsing/replacement, item correlation markers, activity-chain inspection, and mutation receipt construction so Planning uses `plan_audit` while Records retains `record_audit` byte and response compatibility.
- [x] 2.5 Extract only the mutation, query, history, and governance seams required by both profiles behind existing Records facades; do not copy Records modules or rename public Records contracts.
- [x] 2.6 Run `uv run --frozen python -m pytest -q tests/test_structured_collections.py tests/test_record_formats.py tests/test_record_mutation.py tests/test_record_audit_protocol.py tests/test_record_governance.py tests/test_record_memory_command.py` and correct every regression before adding Planning product behavior.

## 3. Planning Schema And Relationship Semantics

- [x] 3.1 Implement strict Planning-profile validation for the exact item/descriptor bounds, kind/status/lifecycle cross-field rules, conditional priority/commitment/horizon requirements, health, dates, tags, and manifest-declared domain extensions.
- [x] 3.2 Implement explicit minimal-capture defaults (`work-item`, `candidate`, active lifecycle, `none`, `uncommitted`, `inbox`, `unknown`) without parsing or classifying title/body text.
- [x] 3.3 Implement same-collection area and parent validation for area, outcome, initiative, and work-item semantics, including candidate/considering omissions, committed-parent requirements, explicit-area consistency, kind checks, cycle refusal, and live-member/child checks before area or parent archival.
- [x] 3.4 Implement exact bounded `progress_evidence` `{collection, role, view}` validation and round-trip without resolving the Record collection, checking the saved view, or evaluating progress; defer inline Records queries to Review.
- [x] 3.5 Implement exact bounded `{kind, ref, label?}` external-execution pointers, rejecting phase/health/copied execution payloads and avoiding local-path, stable-ID, URL, or remote-system resolution.
- [x] 3.6 Implement report-only Planning inspection for state, schema, identity, dates, hierarchy, templates, and agent-audit gaps while leaving canonical files untouched and requiring human repair of invalid direct edits.
- [x] 3.7 Run the pure Planning schema/reference/relationship/inspection tests and correct all failures before public-command wiring.

## 4. Guarded Planning Mutation

- [x] 4.1 Write failing `create`, `add`, `update`, and `triage` tests for the exact action matrix, create-only refusal, exact replay, ID conflict, required reasons, property plus complete-body update, constrained triage, missing/ambiguous targets, stale container/item guards, and no title/fuzzy fallback.
- [x] 4.2 Implement Planning collection creation with a strict human-readable manifest, optional empty Markdown-item source, default saved views, ordinary template descriptors, and complete pre-publication cleanup on interruption.
- [x] 4.3 Implement guarded Planning add through the shared Markdown-item writer with stable UUID identity, optional body, schema/relationship validation, same-vault serialization, idempotency, audit correlation, and guarded batch publication.
- [x] 4.4 Implement targeted Planning property and complete-body update through the shared writer, preserving untouched files/bytes and refusing no-op, stale, missing, ambiguous, invalid-direct-edit, or partially authorized snapshots.
- [x] 4.5 Implement triage as an active-lifecycle constrained explicit transition over only kind/status/priority/commitment/horizon/area/parent with the same drift guards, validation, history, idempotency, and terminal receipt path.
- [x] 4.6 Add caught-error rollback, abrupt-publication-gap, CRLF/BOM/no-final-newline, case-insensitive collision, same-vault contention, and separate-vault independence tests for Planning without weakening the existing Windows publication contract.
- [x] 4.7 Prove a direct human edit is immediately visible, produces a bounded positive audit gap, is never repaired by Planning inspection, and makes a stale agent mutation refuse.

## 5. Multi-Horizon Query And Derived Views

- [x] 5.1 Write failing tests for exact authored horizon views, independent date windows, lifecycle selection, deterministic sort, projection, bounded grouping, pagination/continuation, hierarchy modes/caps, saved-view composition, and JSON/Markdown/CSV provenance.
- [x] 5.2 Scaffold `inbox`, `week`, `month`, `quarter`, `year`, and `multi-year` saved views over one schema, with no automatic rebucketing, implicit view synthesis, or horizon-specific canonical stores.
- [x] 5.3 Route Planning rows through the shared bounded query evaluator and snapshot continuation machinery while mapping profile-specific reserved fields and preserving Records query behavior.
- [x] 5.4 Implement exact `ancestors`/`descendants` authorized hierarchy expansion with depth 8/node 500 hard caps, deterministic ordering, separate nodes/edges, and explicit truncation; do not compute progress percentages, dependencies, capacity, critical paths, or inferred blockers.
- [x] 5.5 Implement provenance-bearing non-persistent Planning renderings that identify the exact collection/query/view/snapshot/generation time and never write summaries, roadmaps, dashboards, or exports.
- [x] 5.6 Run Planning query/view/scale tests plus the existing Record/dataset query suites.

## 6. Governance Before Planning Reduction

- [x] 6.1 Write failing L0–L6 Planning tests for withheld manifests, authorization-before-identity UUID discovery, mixed-release item files, hidden malformed/duplicate/cap-consuming items, hidden-only continuation stability, and partial-view mutation refusal.
- [x] 6.2 Write failing non-disclosure tests proving hidden items cannot affect horizon totals, grouping, ordering, tree shape, truncation, ambiguity, error wording, evidence pointers, execution pointers, history, or receipts.
- [x] 6.3 Implement Planning manifest/item authorization so paths pass L6 before parsing identity or content and only authorized items enter snapshots, caps, schema/relationship validation, query, grouping, hierarchy, or rendering.
- [x] 6.4 Implement shape-specific default-deny Planning projectors for rows, relationships, evidence, execution pointers, paths, IDs, hashes, audit/history, conflicts, continuations, groupings, derived provenance, and terminals.
- [x] 6.5 Implement Planning precommit disclosure/receipt integration inside the guarded mutation, before guarded batch publication, while keeping governance receipts, Planning audit events, operational journals, and terminal receipts distinct.
- [x] 6.6 Run focused governance, release-gate, excluded-tier, stable-reference, receipt, mutation, and Planning reduction tests and correct every leak or public-shape discrepancy.

## 7. Recall Isolation, Freshness, And Reconciliation

Primary ownership: `src/exomem/recall_policy.py`; bump `RECALL_POLICY_VERSION` from the Records-only policy. Existing lexical/vector/graph/claim/freshness consumers already use the centralized predicates and SHALL change only where a failing test proves a bypass.

- [x] 7.1 Write failing high-cardinality tests proving one thousand raw Planning items and generated views do not enter ordinary recall while a strictly valid `_collection.md` remains discoverable and explicit `plan_memory` queries remain complete within caps.
- [x] 7.2 Extend the one centralized structured-only recall policy to exact Planning paths and reuse it across every current/incremental lexical, unit, vector, graph, claim, relation, filter-only, auto-widen, warmup, watcher, move/delete, audit, reconciliation, and final-egress path.
- [x] 7.3 Preserve identity/resolver freshness for all Planning files while projecting recall freshness over eligible manifests so raw-item edits do not churn recall caches and manifest or policy changes do.
- [x] 7.4 Extend model-free semantic purge and maintenance reconciliation to remove stale Planning lexical/unit/vector/graph/claim/deferred rows without deleting canonical files, Planning references, resolver identity, or audit history.
- [x] 7.5 Run `uv run --frozen python -m pytest -q tests/test_records_recall_policy.py tests/test_records_recall_freshness.py tests/test_records_recall_index_sync.py tests/test_records_recall_lexstore.py tests/test_records_recall_graph.py tests/test_records_recall_claims.py tests/test_records_recall_reconcile.py` plus the new Planning variants, including policy-only invalidation and final-candidate defenses.

## 8. One Generated Planning Command

Primary ownership: new `src/exomem/plan_memory.py`, `src/exomem/commands.py`, `src/exomem/command_surface.py`, `src/exomem/governance/egress.py`, `tests/fixtures/mcp_tool_schemas.json`, `src/exomem/tool_surface_contract.json`, and `docs/capabilities.md`. Update the repository's canonical command spec, product spec/metadata, and selector classification from one Python leaf/signature; do not create duplicate surface implementations.

- [x] 8.1 Write failing action-matrix and dispatcher tests for the exact fields/types/defaults/caps/result/error shapes of `inspect`, `create`, `query`, `add`, `update`, and `triage`, including saved-view/hierarchy composition and preservation of explicit false/empty/zero values.
- [x] 8.2 Implement `plan_memory` once in the canonical command registry, with `inspect`/`query` lease-free and `create`/`add`/`update`/`triage` writer-routed; reject unknown selectors fail-closed.
- [x] 8.3 Add selector-aware invocation classification, hosted admission, idempotency, retry, terminal-response, governance-projector, and writer-lease coverage without adding Planning-specific hosted routing.
- [x] 8.4 Run `PYTHONPATH=src python scripts/dump-tool-schemas.py` and `uv run --frozen python scripts/generate-capabilities.py`, deliberately review the schema/tool-fingerprint diff, and treat the script's external ChatGPT Personal Plugin attestation warning as release-blocking rather than silently updating it.
- [x] 8.5 Run `uv run --frozen python -m pytest -q tests/test_mcp_schema_fidelity.py tests/test_rest_registry.py tests/test_rest_api.py tests/test_cli_ops.py tests/test_tool_surface_contract.py tests/test_command_surface_retry.py` plus Planning command tests, then `uv run --frozen python scripts/generate-capabilities.py --check`.

## 9. Guidance, Packs, And Documentation

- [x] 9.1 Update portable bootstrap and the hand-authored generic skill scaffold/reference so goals, bugs, features, priorities, horizons, and roadmap questions route naturally to `plan_memory`, while Records, Review, Notes, and OpenSpec boundaries remain explicit.
- [x] 9.2 Update relevant technical, business, health, personal, and creative pack guidance with optional Planning fields/views/templates and simple verbs; selecting a pack SHALL create nothing and SHALL NOT fork core schemas.
- [x] 9.3 Do not add a thin command-alias Planning workflow skill; document that a future weekly/quarterly Review or collection-adoption workflow may justify one as a separate product change.
- [x] 9.4 Update README, product model, capabilities, knowledge-pack guidance, and dedicated Planning documentation with the canonical schema, six actions, human editing, horizons, relationships, evidence/execution pointers, derived-output provenance, compatibility, and deferred Review/UI boundary.
- [x] 9.5 Run bootstrap, pack, scaffold integrity/no-leak, generated-doc, and capability checks.

## 10. Real Product Paths, Review, And Delivery

- [x] 10.1 Add an end-to-end generated dispatcher journey for software intent: create collection, capture bug/feature candidates, build outcome→initiative→work-item, triage across horizons, link thin OpenSpec/repository pointers, guarded-update one item, query derived views, and preserve restart identity.
- [x] 10.2 Add the materially different non-software journey with multi-year intent, shorter initiative, domain extension, Records saved-view evidence pointer, direct edit, and the same generic Planning semantics.
- [x] 10.3 Extend the installed-wheel stdio product loop with both journeys, restart persistence, direct-edit visibility plus positive audit gap, raw-item recall isolation, and no source-tree imports or optional models; run installed CLI inspect/query against the stdio-created state and compare identity/snapshot semantics.
- [x] 10.4 Extend the installed auth-required HTTP gate with a protocol-valid unauthenticated Planning request that asserts the exact authentication refusal and raw-byte non-disclosure.
- [x] 10.5 Run strict change and canonical-spec validation, all focused Planning/Records/governance/recall suites, generated-surface checks, Ruff, mypy, scaffold privacy, capability generation, packaging, and the installed product loop.
- [x] 10.6 Run the repository lean full suite and proportional latency/scale gates; attribute any failure from exact traces rather than weakening unrelated timing or governance tests.
- [x] 10.7 Run an independent architecture/code/security review focused on duplicate collection mechanics, Records compatibility, profile leakage, state coherence, stale/ambiguous writes, hierarchy cycles, hidden-target disclosure, pre-reduction governance, recall flooding, derived source-of-truth promotion, external-pointer resolution, direct edits, audit gaps, caught-error rollback, and Windows batch publication.
- [x] 10.8 Correct every material reviewer finding and rerun affected plus full verification; then run an independent installed-product verifier through both public journeys.
- [x] 10.9 Capture durable architecture decisions, delivered behavior, compatibility, verification, deferred Review/UI work, and any discovered failure modes back into Exomem with links to the prior Planning and Records notes.
- [x] 10.10 Commit only intended scope, integrate current `origin/main` in the feature worktree, rerun required verification, push, and open a ready Conventional-Commit pull request without merging unless separately authorized.

## Verification Evidence (2026-08-11)

- Strict change validation and all 36 canonical OpenSpec specs passed after integrating current `origin/main`. The post-main focused Planning/Records compatibility band passed 288 tests, and the generated-surface/bootstrap/plugin/scaffold band passed 147 tests.
- MCP/CLI registry classification, plugin/scaffold sync, capability documentation, wheel and sdist builds, targeted Ruff and mypy, and `git diff --check` passed. The post-main installed-wheel product loop passed in 28.6s across software and non-software Planning, restart/direct edits, CLI, stdio MCP, HTTP refusal, and writer-lease coordination.
- Independent code review approved the corrected diff with no blocker/high/medium findings. Independent product verification accepted it with one external gate.
- The decisive post-main lean-suite remainder, using a short owner-trusted temp root and deselecting only the external connector attestation, passed 8,190 tests with 134 expected skips and zero failures in 803.04s.
- Release remains blocked at `tests/test_connector_guardrails.py::test_chatgpt_personal_plugin_tracks_current_tool_surface_rollout`: the live 29-tool surface SHA-256 is `9c374e27960b00df36722d355037298a17847c2f01ac1ba359e482197cede824`, while the external Personal Plugin attestation still declares pending `e989c9a5…`. Do not change that attestation without the required external connector refresh and verification.
- Intended implementation commit `99b2fc68` and current-main integration commits `74f97a2d` and `c74d2141` were pushed to ready PR #418 with Conventional-Commit title `feat(planning): add multi-horizon planning`; the PR remains unmerged pending separate authorization.
