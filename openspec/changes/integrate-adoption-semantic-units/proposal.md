# Proposal: integrate-adoption-semantic-units

## Why

Adoption Studio (0.24.0) and the semantic-units layer (semantic write contract, `observe_memory`, semantic unit context packs — 0.23.0/0.24.0) shipped in the same release window without knowing about each other: agents drafting adoption proposals never see the semantic-unit context packs for the sources they cite, contract violations surface only at apply time (after the reviewer already approved), and the Studio shows approvers nothing about the reviewed-none disposition their approval will record. The apply-side integration (validate → draft → `reviewed_none` commit, landed in PR #234's merge resolution) exists in code but not in the spec.

## What Changes

- `adoption_studio(action="work-item")` includes the semantic-unit context pack for each bound source (bounded, read-only), so agents can compile with unit-level context instead of raw excerpts alone.
- `adoption_studio(action="propose")` runs the semantic write contract's validation for compilation/supersession payloads at submission: blocking findings are recorded on the proposal (`contract_findings`), so an invalid compilation is visible in the review queue instead of failing at apply.
- Compilation proposal payloads MAY carry semantic blocks in `content`; propose-time validation checks them with the same rules the create path enforces.
- Studio proposal detail renders contract findings and states the reviewed-none consequence ("no typed relation yet — this page will resurface in the relation-debt queue") before the approve button.
- The `reviewed_none` apply flow (validate → bind draft → commit with `relation_disposition="reviewed_none"`, `relation_review_hash`, approver `why` as review reason) becomes a spec requirement rather than incidental behavior.
- No server-side LLM anywhere; Studio assets stay inert no-build JS; no new `adoption_studio` parameters (golden schema untouched) — validation rides existing payloads.

## Capabilities

### New Capabilities

(none)

### Modified Capabilities

- `adoption-studio`: work-item gains semantic-unit packs; propose gains contract validation with recorded findings; apply-proposal's reviewed-none flow and the Studio findings surface become requirements.
- `context-packs`: semantic unit context packs gain the adoption work-item as a governed consumer (bounded pack inclusion contract).
