# Tasks — amend-epistemic-bench-families

## 1. Stage 1 Red — contracts and drift check

- [x] 1.1 New tests in `tests/` (benchmarks-protocol scope): `preregistration-amendment-receipt.v1` parsing and validation (shape, required fields, founder acknowledgment); chain-fold validation from the pinned base sha (missing / out-of-order / mismatched receipt each refuses with its typed error); the ratified-identity drift check (byte-identical base passes; receipted chain passes; anything else is a named failure quoting expected and actual identities); run-manifest lineage block (absent = valid today; present = ordered, culminating in the effective sha; unreceipted working state refuses manifest construction).
- [x] 1.2 Run the new tests and confirm red (no amendment-receipt model, no chain validation, no drift check exist yet).

## 2. Stage 1 Green — protocol implementation

- [x] 2.1 `benchmarks/protocol/contracts.py`: `AmendmentReceipt` model (`preregistration-amendment-receipt.v1`), discovery beside `ratification.v1.json`, chain-fold validation anchored at `RATIFICATION_CONTRACT_SHA256`, typed refusals.
- [x] 2.2 `benchmarks/protocol/schema/run-manifest.v2.schema.json`: optional additive `preregistration_lineage` block (ordered amendment receipt identities + effective sha); construction refuses on unreceipted working state.
- [x] 2.3 Drift check wired where contract identities are already asserted (contracts load path + the existing receipt-integrity test surface), so a stale or silently-edited pre-registration is a named finding.
- [x] 2.4 Stage-1 suite green; paste verbatim output.

## 3. Amendment text — f15–f19 (content, then founder acknowledgment)

- [x] 3.1 Author the dated, reasoned §7 Amendment entry in `benchmarks/epistemic/PREREGISTRATION.md`: families f15 `prediction_window`, f16 `plan_record_linkage`, f17 `derivation_collapse`, f18 `negative_result_retention`, f19 `loop_composite` — each with kind, public-coverage statement, core assertions, and ≥2-representation acceptance predicates in the §1/§4 style; new assertion names appended to the §2 registry; f18's catastrophic-set candidacy stated as a proposal for explicit accept-or-strike at acknowledgment; the stale "Status: PROPOSED" header corrected to reflect ratified-plus-amendments state within this same amendment.
- [x] 3.2 Compute the amended file sha256 and author `benchmarks/epistemic/contracts/amendment-2026-08-loop-closure.v1.json` with base sha, amended sha, date, and reason — acknowledgment fields left for the founder.
- [ ] 3.3 **Founder-owned:** Hugo reviews the amendment entry, decides f18's catastrophic-set candidacy (`catastrophic_set_decision`: accept | strike), and completes the acknowledgment fields (`ratifier`, `acknowledged_on`, `repository_revision` = the commit containing the amended pre-registration + pending receipt). In the SAME commit, update the two pending-state tests that deliberately trip on acknowledgment (`tests/test_epistemic_amendment_governance.py` — the real-receipt tests asserting null fields and the pending refusal). No comparative run may rely on f15–f19 before this lands.
- [x] 3.4 Chain-fold and drift tests green against the real receipt; paste verbatim output. (The real receipt is intentionally pending; the typed pending-acknowledgment refusal is asserted.)

## 4. Gates

- [x] 4.1 Ruff on changed files; lean pytest for the touched test modules; `openspec validate --specs --strict` green.
- [x] 4.2 Record verification evidence below (test output, shas, receipt path).
