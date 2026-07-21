# Tasks: integrate-adoption-semantic-units

## Lane A — engine (adoption_proposals.py; executor: opus)

- [x] A.1 Red-first in `tests/test_adoption_proposals.py`: `test_work_item_rows_include_semantic_unit_packs` (source with units → row `semantic_units` populated and bounded; source without units → explicit empty pack), `test_work_item_pack_assembly_is_read_only` (byte-snapshot vault before/after), `test_propose_records_contract_findings_for_reviewable_gaps` (compilation w/o typed relation → status `proposed`, `contract_findings` non-empty, `reviewed_none_required` true), `test_propose_invalidates_on_non_review_blockers` (a finding no review can clear → status `invalid`), `test_queue_and_context_carry_contract_findings` (`_item_view` + `assemble_context` shapes), `test_reviewed_none_review_reason_is_approver_why` (regression pin on landed behavior).
- [x] A.2 Implement per design 1: `semantic_units` per source row in `work_item`, reusing the context-pack constructor (find it via `semantic_unit_read` imports; do not reimplement), bounded by the clamped `max_chars_per_source`.
- [x] A.3 Implement per design 2: propose-time `validate_only` for compilation/supersession; persist compact `contract_findings` + `committable_after_review`/`reviewed_none_required`; classify invalid vs proposed. Confirm validate_only writes nothing (assert in A.1's read-only test).
- [x] A.4 Implement per design 3: `contract_findings` in `_item_view` and `assemble_context`.

## Lane B — Studio UI (studio assets; executor: opus; file-disjoint from Lane A)

- [x] B.1 Red-first `tests/test_studio_adoption_ui_model.py`: findings-rendering model function (compact findings list → display lines; `reviewed_none_required` → fixed consequence sentence; `status=="invalid"` → approve disabled flag).
- [x] B.2 Red-first `tests/browser/adoption.spec.mjs`: proposal detail shows findings lines + consequence sentence; invalid proposal has no enabled approve control (extend the existing suggestions test's mock context with `contract_findings`/`status`).
- [x] B.3 Implement in `src/exomem/studio/adoption-model.v1.js` (pure) + `adoption.v1.js` `proposalSummary`/detail per design 4. Inertness gate (`tests/test_scaffold_no_leak.py`) must stay green.

## Integration verification (orchestrator)

- [x] V.1 `openspec validate --all --strict`; full lean `pytest -q`; browser suite 100%; ruff on changed files; golden schema byte-identical (`git diff --exit-code tests/fixtures/mcp_tool_schemas.json`).
- [x] V.2 One unmocked journey: real vault, propose a compilation without typed relations via the engine, see findings in `review_memory(mode="adoption")`, approve via apply-proposal, confirm reviewed-none disposition + relation-debt resurfacing.
