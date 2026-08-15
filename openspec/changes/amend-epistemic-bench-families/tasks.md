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
- [ ] 3.3 **Founder-owned, and deliberately deferred to `main`.** Hugo reviews the amendment entry, decides f18's catastrophic-set candidacy (`catastrophic_set_decision`: accept | strike), and completes the acknowledgment fields (`ratifier`, `acknowledged_on`, `repository_revision`). In the SAME commit, update the real-receipt test in `tests/test_epistemic_amendment_governance.py` that asserts the null acknowledgment fields. No comparative run may rely on f15–f19 before this lands.

  **This cannot happen on this branch.** `repository_revision` must be a Git ancestor of the run pin (`_git_applicable` → `git merge-base --is-ancestor`), and this repository is squash-merge only, so any branch commit sha is orphaned when the change lands and the pin can never be an ancestor on `main`. The first attempt demonstrated it: it pinned `382d01c9`, which is already not an ancestor of the branch head. The ratification receipt's `7cd15e6d…` is durable precisely because it was recorded after that work landed. So 3.3 is a **follow-up commit on `main` after the squash merge**, pinning the squashed commit that carries the amended pre-registration. Until then the receipt stays pending, which is a supported state and blocks only f15–f19 use (task 3.5).

- [x] 3.4 Chain-fold and drift tests green against the real receipt; paste verbatim output. (The real receipt is intentionally pending. Since task 3.5 the fold *succeeds* while pending and the typed pending-acknowledgment refusal is asserted against the family guard instead.)
- [x] 3.5 Narrow the pending refusal to what the receipt claims: identity derivation, chain folding, drift validation and manifest construction/loading proceed while an amendment is pending; `require_amended_families_released` refuses only a run, score or claim that declares a family the pending amendment introduced. `AmendmentIdentity` records `acknowledgment_status` and derived `introduced_family_ids`; a pending receipt takes its amended-document revision from its uniquely reconstructed introduction commit. Red-first evidence recorded. (design.md Decisions 7–8)

## 4. Gates

- [x] 4.1 Ruff on changed files; lean pytest for the touched test modules; `openspec validate --specs --strict` green.
- [x] 4.2 Record verification evidence below (test output, shas, receipt path).

## Review follow-ups (minor; from the fresh independent review, none blocking)

- ~~Fold-order: `fold_amendment_chain` checks acknowledgment before culmination, so a silently-edited working file reports pending rather than a drift error while the receipt is pending~~ — **resolved by task 3.5.** The fold no longer checks acknowledgment at all, so a silently-edited working file now reports the precise drift error naming expected and actual identities.
- `start_manifest` unreceipted-working-state refusal applies only when `contract_revision` is unpinned; no production caller pins, but the spec scenario reads unconditionally — doc note or follow-up guard. Task 3.5 does not change this: a caller that pins a pre-amendment revision derives an identity with no amendments, so nothing is withheld — but that pinned contract has no f15–f19 in §1 either, so a scenario declaring them has no pre-registration to stand on. A follow-up guard could refuse a declared family absent from the effective contract's §1 table; deliberately out of scope here, since it is a different guarantee ("no undeclared family") from the one being narrowed.
- Ratification-receipt tamper message in exotic 2-event histories says "outside the single allowed pending-to-acknowledged transition" though no transition is allowed for that receipt kind; fail-closed preserved, message imprecise.
- Untested branches: fold "does not culminate in working document" and manifest lineage base-sha/receipt-order mismatches (only effective-sha mismatch is covered).
- Benign unplanned churn noted and accepted: models.py import re-sort/annotation unquoting, `_git` capture_output refactor, exception-type tightening in pre-existing tests.
