# Epistemic State Bench — pre-registration

Status: **PROPOSED** (awaiting founder ratification — ledger task 0.7 of
`add-competitive-benchmark-programme`). On ratification this file FREEZES:
its sha256 at the ratification commit is the pre-registration hash recorded
in every run manifest, and any later change to families, assertions,
predicates, or gates lands only in §7 Amendments, dated and reasoned.
Committed before any competitor has been run by this programme.

Concept vocabulary is anchored to external prior art — PROV-O (provenance),
AGM belief revision (supersession/retraction), Toulmin (claim/warrant/
backing) — not to any product's folder names.

## 1. Scenario families

Kind: `corpus` (phased ingest + agent turns) or `operational` (out-of-band
ops: external_edit, stop_engine, start_engine, fresh_agent, export).
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

(none)
