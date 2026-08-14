# Design — amend-epistemic-bench-families

## Context

The epistemic-state pre-registration (`benchmarks/epistemic/PREREGISTRATION.md`) is founder-ratified: `contracts/ratification.v1.json` records `decision: ratified, ratified_on: 2026-08-11`, and `protocol/contracts.py` pins both the contract sha (`21aa5a88…`) and the receipt sha, with the run-manifest schema requiring the ratification block. The working file is byte-identical to the ratified sha — including its now-historical "Status: PROPOSED" header, which froze inside the ratified bytes and which no check flags. The 2026-08-14 epistemic audit identified five uncovered loop-closure families and mandated contracts-first sequencing: these measurement contracts precede the Tier-1 implementation (`add-epistemic-loop-primitives`) that they will measure.

## Goals / Non-Goals

**Goals:**
- f15–f19 exist as ratifiable §7 Amendment entries with deterministic assertions and acceptance predicates in the registry's existing style.
- Post-ratification amendment governance is mechanical: a receipt shape, chain validation, manifest lineage, and a drift check that names stale or silently-edited pre-registration state.
- The stale Status header is corrected through the same governed path it violates today.

**Non-Goals:**
- No family driver, fixture, corpus, or scoring implementation (follow-up changes own those).
- No change to f01–f14, the catastrophic set's existing members, the G1–G5 gates, or any run semantics for existing families.
- No product runtime, MCP surface, or model use.

## Decisions

1. **§7 Amendments, not file replacement.** The freeze contract prescribes exactly this path for post-ratification change; the pinned base sha remains the ratified identity and amendments extend it. Alternative (re-ratify a v2 file wholesale) rejected: it would orphan the pinned constants and the existing receipt, and would erase the amendment history the freeze exists to preserve.
2. **Amendment receipt mirrors `ratification.v1.json`.** `preregistration-amendment-receipt.v1` carries: base contract sha, amended file sha, amendment date, reason, founder acknowledgment (ratifier + date). Symmetry keeps the validation code one shape family and the founder ceremony identical. Alternative (a single append-only amendment ledger file) rejected: per-amendment receipts match the existing one-artifact-one-receipt pattern and keep each acknowledgment independently verifiable.
3. **Drift-check semantics.** The working file MUST be byte-identical to the ratified base sha, OR equal to the base evolved through a receipted amendment chain (fold: each receipt's `amended_sha256` must equal the file hash after its amendment; the last receipt's hash must equal the current file). Any other state is a named failure. This is what would have caught the stale header the day it mattered.
4. **f18 catastrophic-set candidacy is adjudicated in the amendment text, decided at acknowledgment.** The amendment proposes `negative_result_retention`'s core assertion for the catastrophic set (silently losing a refuted result mirrors `prior_revision_retained`); the founder accepts or strikes it when acknowledging — governance stays with the ratifier, not the author.
5. **Families may describe not-yet-shipped canonical Markdown.** f15/f18 reference prediction units and verdict metadata that Tier-1 will introduce. The pre-registration's own rules already handle this honestly: capability-declared `not_applicable` excludes a family from comparative claims for all providers, and the claim-conditioned rule turns absence into `fail` only where a product's own materials claim the property. Exomem will claim it only once Tier-1 ships — which is the contracts-first point.
6. **Status-header correction rides the first amendment.** The header edit is a content change to frozen bytes, so it must itself be a receipted amendment; folding it into the f15–f19 amendment avoids a ceremony-only receipt.

## Risks / Trade-offs

- **Chain complexity vs one receipt:** the fold validation is a few lines and buys tamper-evidence; accepted.
- **Aggregate suppression blast radius** if f18 joins the catastrophic set: a product silently dropping refuted results suppresses all its aggregates. That is the intended severity, but it is the founder's call (Decision 4).
- **Schema movement:** the run-manifest schema gains an optional amendment-lineage block; consumers pinned to v2 must tolerate its absence (additive, defaulting to base-only lineage).
- **Two sources of family truth during the gap** (amendment accepted, fixtures not yet built): mitigated by the existing "unknown assertion name = fixture load error" rule — nothing can silently run a family the runner does not implement.
