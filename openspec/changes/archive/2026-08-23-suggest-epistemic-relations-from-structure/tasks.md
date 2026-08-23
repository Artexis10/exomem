## 1. Red-First Regressions

- [x] 1.1 Add a failing test proving `- relations: k: [[T]]` yields a lift candidate while a plain `- k [[T]]` bullet inside a block does not.
- [x] 1.2 Add a failing test proving a unit relation the page already promoted under `## Relations` yields no lift candidate, with an unpromoted sibling as the positive control.
- [x] 1.3 Add a failing lift-honesty test proving every emitted relation type appears verbatim as a `raw_relation` on a unit-level edge of the source page.
- [x] 1.4 Add a failing test proving causality, unregistered and out-of-family kinds are refused while an allowed kind on the same unit is taken.
- [x] 1.5 Add a failing test proving lift evidence carries the authoring unit's `unit_ref`, anchor, authored label and resolved family.
- [x] 1.6 Add failing intra-generator aggregation tests for the lift and for shared open questions, proving one candidate per `(target, relation type)` with all matches folded into evidence.
- [x] 1.7 Add a failing test proving shared open questions match across a rich `## Open Question`, a `- category:` override, and a compact `- [question]`.
- [x] 1.8 Add a failing test pinning the ASCII-only `lower()` and trailing-`?` normalization limits.
- [x] 1.9 Add failing tests proving result-adjacency pairs pages whose units answer or resolve the same target, and ignores page-level resolution edges, with a unit-level positive control.
- [x] 1.10 Add a failing test proving structural candidates are bounded and the query count is constant in corpus size.
- [x] 1.11 Add a failing test pinning registration order after the deterministic generators and before the embedding lane.
- [x] 1.12 Add a soft-fail test proving a forced-unavailable snapshot still yields the deterministic candidates, raises nothing, and emits no structural candidate.
- [x] 1.13 Add propose-only and determinism tests proving the vault bytes and graph edges are unchanged and repeated calls agree.
- [x] 1.14 Add a failing dismissal-resurfacing test in the relation-queue suite: dismiss a `shared_open_question` candidate, re-anchor the *other* page's question with this page byte-identical, and require a new fingerprint.
- [x] 1.15 Add a failing test proving an accepted lift bullet re-enters as a page-level edge with `registry_status == "core"`.

## 2. Implementation

- [x] 2.1 Add the family allowlist, registry-status allowlist, resolution-relation set, bound constants and the shared SQL normalization fragment.
- [x] 2.2 Add `_structural_candidates`, opening one validated read snapshot for all three generators and soft-failing to `[]`.
- [x] 2.3 Add `_unit_relation_lift_candidates`: unit-level `semantic_relation` edges with no page-level counterpart, gated by registry status in SQL and by relation family through the registry at call time, aggregated per `(target, authored label)`.
- [x] 2.4 Add `_shared_open_question_candidates`: two UNIONed indexed branches per side, SQL-side normalization, aggregated per target with the other page's unit identity in evidence.
- [x] 2.5 Add `_shared_resolution_target_candidates`: unit-level `answers`/`resolves` edges to a shared target on both sides, aggregated per target with both sides' unit identity and relation kinds in evidence.
- [x] 2.6 Register the shared entry point in `suggest_relations` after the deterministic generators and before the embedding lane, with the truncation interaction recorded in a comment.

## 3. Specifications

- [x] 3.1 Add an ADDED requirement set to `epistemic-graph` covering the three structural methods, the shared snapshot and soft-fail, evidence identity, intra-generator aggregation, bounds and registration order.
- [x] 3.2 Add an ADDED requirement set to `graph-semantic-integrity` covering structural semantic neutrality: no causality, lift-only-what-was-authored, co-participation proposes `relates_to`, and propose-only.

## 4. Verification

- [x] 4.1 Run the new suite plus `tests/test_epistemic_graph.py`, `tests/test_relation_queue.py`, `tests/test_relation_queue_commands.py`, `tests/test_relation_registry.py`, `tests/test_relation_review.py`, `tests/test_markdown_relations.py`, `tests/test_semantic_blocks.py`, `tests/test_semantic_units.py`, `tests/test_semantic_unit_graph.py`, `tests/test_epistemic_graph_relation_filter.py`, `tests/test_epistemic_graph_neighbors.py` and `tests/test_audit_relation_debt.py`.
- [x] 4.2 Confirm `tests/fixtures/mcp_tool_schemas.json` and `src/exomem/tool_surface_contract.json` are unchanged.
- [x] 4.3 Run `ruff check` on every changed file.
- [x] 4.4 Run `openspec validate suggest-epistemic-relations-from-structure --strict` and `openspec validate --specs --strict`.

## 5. Independent-Review Fixes

- [x] 5.1 Normalize the lifted label through `relation_registry.normalize_relation`, so a non-canonical authored kind (`Answers:`, `EVIDENCED BY:`) cannot produce a bullet the governed write refuses as `malformed_relation`; add an end-to-end test through `op_connect_memory`.
- [x] 5.2 Amend the `graph-semantic-integrity` delta, which mandated the verbatim label, to require the normalized authored label.
- [x] 5.3 Add a foreign authoring page to the lift-honesty test so relaxing the `source_path` filter is caught; the previous expectation was derived from the same filter and could not fail.
- [x] 5.4 Register the structural generators ahead of the unbounded wikilink generator, pin the new order in both directions, and add a link-heavy-page regression; record the reversal and its reason in `design.md`.
- [x] 5.5 Switch the page-level-edge fixture to the block-body bullet form, so the `src_key <> file_key` guard is what the test actually exercises.
- [x] 5.6 Replace the vacuous evidence-fold assertion with a fixture where the cap bites (eight shared questions, five folded matches, honest total).
- [x] 5.7 Cover the registry-standing gate: a vault extension kind lifts without a code change, and deprecated or scope-violating kinds in the same allowed family do not.
- [x] 5.8 Suppress a later co-participation candidate that duplicates an earlier one's `(target, relation type)`, and add the scenario and a test.
- [x] 5.9 State the principle that distinguishes causality from the eight allowlisted families rather than asserting it.
- [x] 5.10 Update `_scaffold/_Schema/references/operations.md` to list the structural generators.
- [x] 5.11 Re-run the mutation battery: each of the eight behaviours above must fail its own test when reverted.

## 6. Recheck Follow-Through

- [x] 6.1 Guard the normalized label against the canonical relation-bullet grammar itself (probe `markdown_relations._CANONICAL_RE`, do not restate it), applied per row before grouping and before the per-generator cap; cover the over-length key, over-length alias, one-character alias, and non-ASCII alias.
- [x] 6.2 Add the writability clause and scenario to the `graph-semantic-integrity` registry-standing requirement.
- [x] 6.3 Correct the `design.md` residual-risk claim that displaced wikilink candidates are regenerated on the next read. They are not: truncation precedes classification. State what was measured, and that the behaviour is pre-existing and mirror-symmetric rather than introduced here.
- [x] 6.4 File `classify-relation-candidates-before-truncation` as a named follow-up.
- [x] 6.5 File `relation-registry-registers-invalid-aliases` as a named follow-up, with the reproduction: the alias loop records `invalid_alias` without `continue`, so the alias registers anyway and resolves with `alias` standing.
- [x] 6.6 Record in `design.md` that suppression survivor selection is first-generator-wins, so the suppressed candidate's evidence stops feeding any fingerprint and a change confined to the shared-resolution relationship will not resurface a dismissed `relates_to`.

## 7. Orchestrator-owned (NOT for the implementation lane)

- [x] 7.1 After this change merges, sync both delta specs into `openspec/specs/` and archive it with `openspec archive`. It cannot be archived from the implementation lane: the change is not shipped until it is on the default branch, and a sibling lane is authoring specs concurrently, so an early sync would collide in `openspec/specs/`.
