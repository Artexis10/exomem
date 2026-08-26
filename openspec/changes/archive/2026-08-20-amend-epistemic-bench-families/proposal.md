# Amend epistemic bench families

## Why

The 2026-08-14 whole-system epistemic architecture audit found five loop-closure capabilities that the epistemic-state pre-registration's 14 families do not cover: prediction-window staleness, plan↔record linkage, derivation double-counting, negative-result retention, and a composite Records+Planning+Review journey. The programme is contracts-first — these measurement contracts must be accepted before Tier-1 implementation (`add-epistemic-loop-primitives`) so the pre-registered tests precede the machinery they measure. The pre-registration is already founder-ratified (2026-08-11, receipt `benchmarks/epistemic/contracts/ratification.v1.json`; the working file is byte-identical to the pinned sha `21aa5a88…`), so additions take the §7 Amendments path — which today lacks an amendment-receipt shape, and nothing checks the working file's relationship to its ratified identity (its own "Status: PROPOSED" header froze stale inside the ratified bytes and no check flagged it).

## What Changes

- File five scenario families **f15–f19** as dated, reasoned §7 Amendment entries in `benchmarks/epistemic/PREREGISTRATION.md`: `prediction_window`, `plan_record_linkage`, `derivation_collapse`, `negative_result_retention`, `loop_composite` — each with kind, public-coverage statement, core assertions, and ≥2-representation acceptance predicates in the registry's existing style; catastrophic-set candidacy for `negative_result_retention` adjudicated explicitly in the amendment text.
- Add new deterministic assertions to the registry where the families need them (e.g. `due_prediction_surfaced`, `divergence_surfaced_without_mutation`, `support_collapse_inspectable`, `refuted_retrievable_at_full_standing`), all runnable against neutral state snapshots, no judge.
- Introduce **`preregistration-amendment-receipt.v1`** mirroring `ratification.v1.json`: base contract sha, amended file sha, amendment date, reason, founder acknowledgment. `benchmarks/protocol/contracts.py` loads and validates the amendment chain; the run-manifest schema records base identity plus amendment lineage.
- Correct the stale "Status: PROPOSED" header within the same receipted amendment (the header is part of the frozen bytes; the pinned ratified sha remains the base identity).
- Add the missing **drift check**: a test asserting the working pre-registration is byte-identical to the ratified base sha OR equals base plus a receipted amendment chain — a stale or silently-edited pre-registration becomes a named failure, not silence.

## Capabilities

### New Capabilities

(none)

### Modified Capabilities

- `epistemic-state-bench`: family set extends f01–f14 with f15–f19; assertion registry gains the new deterministic assertions; amendment governance becomes explicit (post-ratification changes require a receipted §7 amendment).
- `benchmark-protocol`: contracts module and run-manifest schema carry the amendment receipt chain alongside the existing ratification receipt.

## Impact

- `benchmarks/epistemic/PREREGISTRATION.md` (§7 entries + header correction) and `benchmarks/epistemic/contracts/` (new amendment receipt artifact).
- `benchmarks/protocol/contracts.py` and `benchmarks/protocol/schema/run-manifest.v2.schema.json` (amendment lineage).
- Tests: contract-loading coverage for the amendment receipt; the pre-registration drift check.
- No product runtime code, no MCP surface change, no model use — families are deterministic fixture + assertion work (pure-substrate: nothing here runs a model; nothing is default-on or heavy; the offline runner behavior is unchanged until fixtures land in their own follow-ups).
