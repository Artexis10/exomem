# Epistemic State Bench — pre-registration

Status: **RATIFIED + AMENDMENT LINEAGE** (base founder-ratified 2026-08-11;
post-ratification amendments are governed by their receipts). The
ratified base is FROZEN: its sha256 at the ratification commit is the base
pre-registration hash recorded in every run manifest, and any later change
to families, assertions, predicates, or gates lands only in §7 Amendments,
dated, reasoned, and receipted. Committed before any competitor has been run
by this programme.

Concept vocabulary is anchored to external prior art — PROV-O (provenance),
AGM belief revision (supersession/retraction), Toulmin (claim/warrant/
backing) — not to any product's folder names.

## 1. Scenario families

Kind: `corpus` (phased ingest + agent turns), `operational` (out-of-band
ops: external_edit, stop_engine, start_engine, fresh_agent, export), or
`corpus + operational` when both are necessary to the property.
Coverage: public-suite overlap executed as a subtraction — overlapping
families report STATE metrics only; answer accuracy for them is deferred to
the public-suite lanes.

| id | family | kind | public coverage | core assertions |
|---|---|---|---|---|
| f01 | explicit_correction | corpus | partial: LongMemEval knowledge-update → state-only | exactly_one_current_revision · prior_revision_retained · revision_links_to_predecessor · no_retired_state_served_as_current |
| f02 | implicit_staleness | corpus | partial: LongMemEval temporal-reasoning; STALE (paper) → state-only | no_retired_state_served_as_current · exactly_one_current_revision · uncertainty_declared |
| f03 | conflicting_sources | corpus | partial: MemoryAgentBench conflict-resolution → state-only | contradiction_visible · contradiction_not_flattened |
| f04 | source_quality_asymmetry | corpus | none | contradiction_visible · uncertainty_declared · evidence_path_resolves |
| f05 | supersession_lineage | corpus | none | exactly_one_current_revision · prior_revision_retained · revision_links_to_predecessor · evidence_path_resolves |
| f06 | evidence_before_belief | corpus | none | evidence_path_exists · evidence_path_resolves (raw source and derived claim remain distinct snapshot kinds) |
| f07 | decision_vs_hypothesis | corpus | none | decision_distinguishable_from_hypothesis |
| f08 | modeled_ignorance | corpus | none | open_question_queryable · uncertainty_declared |
| f09 | abstention_insufficient_support | corpus | partial: LongMemEval abstention → state-only | uncertainty_declared · fresh-agent abstention with own-declared basis |
| f10 | downstream_impact | operational | none | dependent_conclusions_surfaced_for_review · no_retired_state_served_as_current (dependents not silently rewritten) |
| f11 | triage_invalidation | operational | none | review_state_durable · review_reopens_on_material_change · review_stays_closed_on_irrelevant_change |
| f12 | external_canonical_edit | operational | none | external_edit_authoritative_within(bound) · no_retired_state_served_as_current |
| f13 | engine_off_portability | operational | none | 13a: export_reconstructs_state(tolerance) — scoreable for all; 13b: engine-off legibility — pass/fail for file-canonical products, capability-declared otherwise (marketing-claim check applies) |
| f14 | cross_agent_continuation | operational | none | continuation probe (deterministic citation checks from the agent's declared basis; judged narrative gated on blinding) · no_cross_case_residue |
| f15 | prediction_window | corpus | none | due_prediction_surfaced · verdict_state_retrievable |
| f16 | plan_record_linkage | corpus + operational | none | divergence_surfaced_without_mutation |
| f17 | derivation_collapse | corpus | none | support_collapse_inspectable |
| f18 | negative_result_retention | corpus | none | refuted_retrievable_at_full_standing |
| f19 | loop_composite | operational | none | loop_journey_state_coherent (goal → hypothesis → prediction → intervention → records → review → revision across ≥3 sessions and one engine restart; system state, not retrieval accuracy) |

## 2. Assertion registry (deterministic; unknown name = fixture load error)

```
exactly_one_current_revision          no_retired_state_served_as_current
prior_revision_retained               revision_links_to_predecessor
evidence_path_exists                  evidence_path_resolves
contradiction_visible                 contradiction_not_flattened
decision_distinguishable_from_hypothesis
open_question_queryable               uncertainty_declared
review_state_durable                  review_reopens_on_material_change
review_stays_closed_on_irrelevant_change
external_edit_authoritative_within    export_reconstructs_state
dependent_conclusions_surfaced_for_review
no_cross_case_residue
due_prediction_surfaced              verdict_state_retrievable
divergence_surfaced_without_mutation support_collapse_inspectable
refuted_retrievable_at_full_standing loop_journey_state_coherent
```

All run against neutral state snapshots (never product internals or answer
prose); text comparison uses the harness's single fixed matching rule.
Deterministic results are final; no judge may overturn one.

## 3. Catastrophic set (any failure ⇒ INTEGRITY FAIL; all aggregates suppressed)

`no_retired_state_served_as_current` · `prior_revision_retained` ·
`evidence_path_resolves` · `contradiction_not_flattened` ·
`no_cross_case_residue` · `external_edit_authoritative_within`

## 4. Acceptance predicates (≥2 structurally different representations pass)

- **prior_revision_retained**: engine revision chain/history API; OR a
  retained superseded artifact reachable via the documented interface; OR
  VCS history reachable through the product's documented workflow (scored in
  a disclosed `native-git` row).
- **revision_links_to_predecessor**: explicit supersession edge/field; OR an
  in-content reference naming the replaced item; OR an API version chain
  (parent/root/next identifiers).
- **contradiction_visible / not_flattened**: typed contradiction relation;
  OR both items retrievable as current-relevant by the same query with a
  conflict marker; OR a review-queue conflict entry. Flattening = exactly
  one side retrievable as settled truth with the other silently gone.
- **decision_distinguishable_from_hypothesis**: type/kind field; OR a
  documented collection/folder convention; OR a schema/metadata attribute —
  any mechanism a fresh agent can query through the documented interface.
- **open_question_queryable**: dedicated type; OR tag/attribute reachable by
  query; OR task/review queue entry.
- **external_edit_authoritative_within**: watcher/auto-resync inside the
  documented freshness bound; OR a documented manual resync command (scored
  with its longer, disclosed bound).
- **export_reconstructs_state**: canonical files are the state; OR export
  endpoints covering items, currency, lineage, and evidence edges within the
  declared tolerance.
- **evidence_path_{exists,resolves}**: provenance frontmatter/fields; OR
  typed relations; OR API source-document linkage. Resolution means every
  hop dereferences to a live artifact.
- **due_prediction_surfaced**: a deadline/status query over prediction
  units returns the overdue item; OR a due/review queue derived from dated
  prediction artifacts surfaces it after the harness advances the clock.
- **verdict_state_retrievable**: verdict metadata on a hypothesis/prediction
  is queryable through the documented interface; OR a linked outcome/result
  artifact carries the verdict and resolves back to what it adjudicates.
- **divergence_surfaced_without_mutation**: a review queue contains a
  plan-versus-records divergence item while the plan identity is unchanged;
  OR a documented comparison/query returns the divergence while a before/
  after snapshot proves that the plan was not auto-mutated.
- **support_collapse_inspectable**: a provenance graph/API exposes the shared
  source root behind both derived notes; OR documented source references on
  the notes let the harness resolve both support paths to the same source.
- **refuted_retrievable_at_full_standing**: a verdict/status query returns
  the refuted hypothesis without lifecycle demotion; OR the ordinary
  hypothesis interface retrieves it with refuting evidence and metadata that
  distinguishes `refuted` from both active-unresolved and superseded state.
- **loop_journey_state_coherent**: typed records/collections preserve every
  journey stage and its documented links through restart; OR ordinary
  artifacts plus API/content references reconstruct the same ordered journey
  after restart. In both cases deterministic snapshots must show ≥3 sessions,
  one restart, the review outcome, and the resulting revision.

Claim-conditioned N/A: a product whose own materials claim a property scores
**fail**, not not_applicable, when it is absent — with the claim cited.
Any not_applicable in a family excludes that family from every comparative
claim, for all providers.

## 5. Controls

`grep-markdown` (ripgrep over the raw corpus + the same fresh answer agent)
and `no-memory` run in every table. If a control passes an invariant, that
invariant's pass carries no product signal and the table says so.

## 6. Strategy decision gates (PROPOSED thresholds; frozen with this file)

| Gate | Metric | Threshold | On failure |
|---|---|---|---|
| G1 standard-memory parity | 25-case LongMemEval-S tier, shared reader+official judge | exomem-native ≥ hybrid-rag-control AND ≥ bm-local − 10 pts in both harness families | "bounded deficit" verdict permitted only with a named, fixable cause in the failure taxonomy; otherwise NARROW |
| G2 epistemic wedge | no-N/A families, deterministic assertions | ≥3 families where exomem passes all and each competitor variant fails ≥1; AND zero exomem catastrophic failures anywhere | wedge unproven → G5 decides NARROW vs STOP |
| G3 lifecycle cost | per-journey native-mode cost, symmetric metering | exomem-native ≤ 3× supermemory-local-native | premium must be explicitly justified by G2 evidence or NARROW |
| G4 ops parity floor | install-to-healthy, first-answer, restart-recovery | not worst-of-three by >2× on any; zero silent state corruption | fix before broad positioning; corruption ⇒ STOP-level defect |
| G5 founder-replacement | written outside-reviewer answer + founder usage evidence | replacing exomem with a competitor demonstrably loses valued properties | if replacement is lossless → STOP productizing (keep as personal tooling) |

Decision mapping: CONTINUE broadly iff G1∧G2∧G4. NARROW to the epistemic
wedge for evidence-heavy individuals/small teams iff G2∧¬G1. STOP
productizing iff ¬G2 on adversarially-clean runs. Sunk cost is not an input;
the only admissible continuation rationale is that the wedge survives
adversarial comparison at acceptable marginal cost.

## 7. Amendments

- 2026-08-09 — `evidence_path_resolves` semantics sharpened pre-ratification: a promoted conclusion with ZERO evidence hops is an unresolvable path (vacuity fails). Rationale: erasing evidence edges must never be cheaper than maintaining them; §4's 'every hop dereferences' text is hereby extended to require at least one hop for promoted conclusions. Blast radius acknowledged: an unsourced promoted conclusion is an integrity failure with every aggregate suppressed. `evidence_path_exists` remains the non-catastrophic co-assertion; both fail on zero hops by design.

- 2026-08-15 — **Loop-closure family amendment.** Reason: the 2026-08-14 whole-system epistemic architecture audit found five unmeasured loop-closure properties, and the programme requires their deterministic measurement contracts before the product primitives they assess. This amendment corrects the stale ratification status header; extends §1 with f15 `prediction_window` (corpus; no public coverage; overdue prediction then resolving observation), f16 `plan_record_linkage` (corpus + operational; no public coverage; records diverge from a bound plan), f17 `derivation_collapse` (corpus; no public coverage; one source → two derived notes → a third treating both as support), f18 `negative_result_retention` (corpus; no public coverage; refuted hypothesis plus refuting evidence), and f19 `loop_composite` (operational; no public coverage; goal → hypothesis → prediction → intervention → records → review → revision across at least three sessions and one engine restart); appends their deterministic assertions to §2; and adds representation-neutral predicates to §4. All assertions run against neutral state snapshots under the fixed harness matching rule, and claim-conditioned N/A continues to apply. **Catastrophic-set proposal:** add f18's `refuted_retrievable_at_full_standing` to §3 because silently losing or demoting a refuted result destroys negative evidence just as losing a prior revision does. The existing catastrophic set remains unchanged unless the founder records `accept`; the founder must explicitly record `accept` or `strike` in the amendment acknowledgment. Until the receipt is acknowledged, f15–f19 MUST NOT support a comparative run or claim.
