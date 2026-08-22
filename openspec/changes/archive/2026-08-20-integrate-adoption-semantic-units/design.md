# Design: integrate-adoption-semantic-units

## Decisions

### 1. Work-item packs ride the existing source rows

`adoption_proposals.work_item` (src/exomem/adoption_proposals.py, `source_rows` assembly around the `_extract_capture` block) gains a `semantic_units` key per row, built by calling the same pack constructor `context-packs` uses (locate via `semantic_unit_read` / the context-pack module that `#242` added — reuse, never reimplement). Caps: reuse the work item's server-clamped `max_chars_per_source` as the pack character bound; the pack's own item caps stay. Empty → `{"units": [], "available": true}`. No new command parameters (golden schema untouched).

### 2. Propose-time validation reuses the create path's validate_only

In `adoption_proposals.propose`, after `_live_bindings`: for `compilation` and `supersession` kinds, run the SAME two-phase entry the apply path uses — `op_remember(..., validate_only=True)` / `op_replace_memory(..., validate_only=True)` with the payload mapped exactly as `_route_apply` maps it. Persist a compact `contract_findings` list (code/severity/detail per finding, plus `committable_after_review`/`reviewed_none_required` booleans) on the proposal record. Classification: findings with `committable_after_review=false` and `committable_without_review=false` → proposal `invalid` (existing findings mechanism); review-resolvable findings → status stays `proposed`, findings recorded. The validate call is read-only (draft registration is ephemeral) — confirm no draft-token persistence leaks from validate_only (it must not create files).

### 3. Findings surface through the existing queue/item shapes

`_item_view` includes `contract_findings` (compact). `assemble_context` already returns proposal findings; extend with `contract_findings` + the reviewed-none consequence booleans so the Studio needs no extra call.

### 4. Studio: render-only change

`adoption.v1.js` `proposalSummary`/detail: render `context.contract_findings` as plain-language lines (reuse the failure-group styling); when `reviewed_none_required`, append the fixed consequence sentence; suppress/disable approve when `context.status == "invalid"` (already refused server-side — this is honesty, not enforcement). Inertness rules hold: no URLs, no storage, generic strings only (`tests/test_scaffold_no_leak.py` gates).

### 5. Reviewed-none spec'd, not changed

`_reviewed_creation` (landed) already implements the required flow; this change adds spec scenarios + a regression test asserting the review reason equals the approver's `why` — no code change expected.

## Risks / constraints

- The validate_only call at propose time doubles validation cost per proposal — acceptable (proposals are low-volume); do NOT cache across proposals.
- Golden schema: no signature changes anywhere. If validate_only shape forces one, STOP and report — that is a design change.
- Hosted admission unaffected (`propose` stays write-classified; `work-item` stays read-only).
