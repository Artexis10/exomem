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
| f20 | structural_emergence | corpus | none | structural_signal_surfaced_within_budget · signal_absence_checked_across_all_surfaces (binds to categories, anchors and disjoint link neighbourhoods, never to vocabulary; four frequency- and length-matched twins: bounded-scope, tangent, deliberate hub, legitimately heterogeneous log) |
| f21 | entity_emergence | corpus | none | entity_candidate_surfaced_from_recurrence · signal_absence_checked_across_all_surfaces (frequency-matched incidental-mention twin; lowercase and non-Latin referents) |
| f22 | unsolicited_contradiction | corpus | none | contradiction_surfaced_unprompted · signal_absence_checked_across_all_surfaces **over contradiction-class signals as well as promotion-class** (concordant-evidence twin in the same similarity band; surfacing route reported, never asserted) |
| f23 | dismissal_respect | operational | none | dismissal_respected_across_passes · counter_emission_not_repeated_per_write (repeated maintenance passes, engine restart, prominence reconfiguration across the full level range; material change still reopens) |
| f24 | fresh_session_reconstruction | operational | none | continuation_packet_reconstructs_session · no_retired_state_served_as_current (scope extended to the packet by amendment sequence 2) |
| f25 | restructure_lifecycle | operational | none | restructure_signal_cleared_by_state_change · signal_absence_checked_across_all_surfaces (cleared by state change, not by dismissal; zero merge-class churn in the frozen window) |
| f26 | hookless_episode_carrier | operational | none | due_state_block_present_in_carrier · continuation_packet_reconstructs_session (end-to-end thin-client episode; due-state block present in the actual compact responses) |

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
signal_absence_checked_across_all_surfaces
structural_signal_surfaced_within_budget
entity_candidate_surfaced_from_recurrence
contradiction_surfaced_unprompted
dismissal_respected_across_passes
counter_emission_not_repeated_per_write
continuation_packet_reconstructs_session
restructure_signal_cleared_by_state_change
due_state_block_present_in_carrier
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

- **signal_absence_checked_across_all_surfaces**: every declared surface —
  audit findings, review queues, proposal queues, and the due-state counters
  block — projects completely and none names the subject under **any signal
  class in the family's declared vocabulary**; OR a documented single "open
  items for X" surface that provably subsumes all four and returns nothing for
  the subject under that same vocabulary. The vocabulary is promotion-class by
  default (structural, split, extract, entity- and identity-candidate) and is
  **widened per family, never narrowed**: f22's twin additionally proves absence
  over contradiction-class signals, and f23's dismissal predicate proves it over
  every class the amendment knows. An empty or missing projection is an
  **error**, never a pass, in both representations.
- **structural_signal_surfaced_within_budget**: a promotion/split/extract
  proposal targeting the note is present in an open review or proposal queue at
  or before the frozen cluster budget; OR a documented structural-suggestion
  query returns it, with the note's own accumulated cluster count observable so
  the budget is measurable rather than assumed.
- **entity_candidate_surfaced_from_recurrence**: an entity-candidate queue entry
  naming the recurring identity; OR a documented "proposed entities" surface
  that resolves back to at least the frozen number of distinct contributing
  sources. Distinct sources, never occurrence counts.
- **contradiction_surfaced_unprompted**: a typed contradiction/conflict queue
  entry naming both items; OR a documented review entry that resolves to both
  and carries a conflict marker. The surfacing route is reported, never asserted.
- **dismissal_respected_across_passes**: a durable triage store keyed by signal
  fingerprint whose decision survives passes, restart, and reconfiguration; OR
  documented per-item review state that resolves to the same fingerprint. A
  changed fingerprint reopening the item is a **pass** in both.
- **counter_emission_not_repeated_per_write**: an emission count observable
  against the batch's write count on the counters block; OR a documented
  batching/debounce statement plus an observed emission count below the write
  count over a bulk batch.
- **continuation_packet_reconstructs_session**: a packet artifact holding every
  seeded unit **by reference** with a documented size budget; OR a documented
  continuation/handoff endpoint whose response resolves each unit back to live
  state. Inlined copies satisfy neither.
- **restructure_signal_cleared_by_state_change**: the original signal is absent
  from every open view while the review store holds no dismissal for it; OR a
  documented signal-lifecycle record showing resolution by state change rather
  than by triage. Merge-class quiet on the new children is required in both.
- **due_state_block_present_in_carrier**: the due-state block is present in the
  compact mutation and recall responses the journey actually received; OR a
  documented compact-response schema that includes the block plus captured
  responses conforming to it. A block only reachable at verbose detail fails.

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

- 2026-08-16 — **No-nudge family amendment (sequence 2).** Reason: the 2026-08-15 no-nudge architecture audit committed the programme to becoming measurably more automatic — structure surfaced without the user asking, entities recognized from recurrence, contradictions surfaced unprompted, dismissals respected forever, fresh sessions reconstructed from durable state, restructures that clear their own signals — and none of it is falsifiable under f01–f19, which measure loop representation and closure rather than autonomous surfacing, nag governance, or the delivery carrier. Contracts-first sequencing is the house rule sequence 1 established: the measurement files before the machinery, so the detectors that follow are built against pre-registered acceptance rather than tuned to pass improvised tests.

  This amendment extends §1 with f20 `structural_emergence` (corpus; no public coverage; a note accumulating structurally distinct durable-unit clusters must surface a promotion-class signal within a frozen cluster budget, with four frequency- and length-matched twins staying quiet: a bounded-scope page, a one-or-two-tangent page, a deliberate hub, and a legitimately heterogeneous log page whose dispersion is intentional), f21 `entity_emergence` (corpus; no public coverage; an identity recurring with reusable facts across at least the frozen number of distinct sources, against a frequency-matched incidental-mention twin, with lowercase and non-Latin referents so script bias is measured rather than hidden), f22 `unsolicited_contradiction` (corpus; no public coverage; materially invalidating evidence surfaces the pair with zero topical agent turns in the trajectory, against a concordant-evidence twin in the same similarity band), f23 `dismissal_respect` (operational; no public coverage; a dismissed fingerprint survives repeated maintenance passes, an engine restart and prominence reconfiguration across the full level range while a material change still reopens, plus counter-emission governance under bulk writes), f24 `fresh_session_reconstruction` (operational; no public coverage; the continuation packet contains every seeded decision unit, open question and latest plan state by reference, excludes a same-sized foreign-project decoy set entirely, and respects a frozen size budget), f25 `restructure_lifecycle` (operational; no public coverage; an applied restructure clears its own signal by state change rather than by dismissal, with zero merge-class churn against the new children inside the frozen window), and f26 `hookless_episode_carrier` (operational journey; no public coverage; a hookless compact-detail surface demonstrates the full carrier path — due-state block present in the actual compact responses, capture landed, reconstruction succeeding). It appends their nine deterministic assertions to §2, headed by the anti-vacuity meta-predicate `signal_absence_checked_across_all_surfaces`, and adds representation-neutral predicates for each to §4. It extends the scenario operation vocabulary with `maintenance_pass`, `triage_decision`, `apply_restructure`, and `configure`; existing families are unaffected.

  **Behaviour, not vocabulary.** The f20 generator asserts that no cluster-name token appears in any assertion parameter, and the synonym-swap fixture requires the signal to survive vocabulary substitution. Negative twins are frequency- and length-matched so raw magnitude can never be the discriminator.

  The corpus binds to the structure it claims: each durable unit carries an opaque **category** and **anchor**, and each page carries a **link neighbourhood** — mutually disjoint neighbourhoods for the accumulating positive, none at all for the bounded twin, one wide neighbourhood for the hub, and a single anchored chain under one uniform category for the deliberate log. Cluster count alone deliberately does **not** separate the set (the log twin carries more clusters than the positive), so no monotone rule over a single count can pass this family; the separation must be a function of the graph and the unit attributes, and it must hold unchanged under the synonym swap.

  **Anti-vacuity.** Every quiet assertion composes `signal_absence_checked_across_all_surfaces`: absence must be established on each declared surface — audit findings, review queues, proposal queues, **and the due-state counters block** — and an empty or missing projection evaluates as an error, never as a pass. Without this, a product that relocates a nag to an unchecked surface, or a projector that silently returns nothing, would pass a quiet assertion.

  Absence is also proven over a declared **signal-class vocabulary**, because relocating a nag across surfaces and renaming it across classes are the same evasion. The vocabulary is promotion-class by default; each family's quiet assertion may widen it and may never narrow it. f22's twin proves absence over contradiction-class signals as well, so a product that surfaced every similar pair as a contradiction produces false positives the family can see rather than false positives invisible to it; f23's dismissal predicate matches a dismissed fingerprint reappearing under **any** class, since a re-nag wearing a different label is the same interruption to the same user.

  **False-positive ceiling.** Zero promotion-class signals on any negative twin is a hard in-family assertion for f20 and f21. The production false-positive budget receives its threshold at calibration and is reported beside, never instead of, the in-family ceiling. No automation metric may be reported without its paired false-positive dual from the same runs, and no aggregate score exists.

  **Constants are calibrated once, then frozen and versioned.** The f20, f21 and f25 budget and window constants are frozen here after a one-time study in which at least three expert annotators label the emergence corpora for the intervention point, taking the median; the protocol and raw labels ship with the judge-agreement assets. Changing a frozen constant requires a new dated §7 amendment with its own receipt, and live judging of the intervention point never occurs in a run. **As filed, the constants in `benchmarks/epistemic/budgets.py` are PROVISIONAL:** the calibration study is blocked on a founder decision about annotator staffing and the small-cohort fallback, and this amendment is withheld, so no run, score or claim can be produced against them. Re-dating this entry with the medians is the act that freezes them.

  **Expected red.** f20, f21 and f22 positives are expected to FAIL on the current runtime. They are falsification targets for the no-nudge programme, not CI failures, and the quiet halves are expected to evaluate as errors rather than passes because the due-state counters surface does not exist yet.

  That expectation binds the f26 carrier driver too. It declares a capability only where a captured compact response evidenced one and marks a surface projected only where a response carried it, so a family whose signal never reached the client evaluates as an error rather than as silence. A journey may not manufacture the quiet it is measuring, and the driver's step list is complete executable argv checked against the envelope's own declared options — a script that cannot run is not evidence about delivery.

  **Catastrophic set: no additions.** Nagging and missed promotions are trust failures, not integrity failures; putting them in §3 would suppress every aggregate over a product-taste miss. This amendment adds **no** catastrophic assertions. It does extend the scope of the existing catastrophic assertion `no_retired_state_served_as_current` to the continuation packet — serving a fresh session a retired decision by reference is the same harm as returning it from a query — and that extension is stated here explicitly so the founder acknowledgment has nothing implicit to adjudicate.

  **The judged half stays out.** Intervention usefulness and continuation sufficiency remain membench agent-track (AT-1) dimensions behind the judge-agreement gate, outside the pre-registered deterministic set. Deterministic gates are never overturnable by a judge.

  Until the receipt is acknowledged, f20–f26 MUST NOT support a comparative run, score, or claim.
