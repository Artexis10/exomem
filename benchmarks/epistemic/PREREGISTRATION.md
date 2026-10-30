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
| f27 | lifecycle_routing_replay | operational | none | lifecycle_consequence_landed_unprompted · no_structured_write_beyond_expectation (a real agent replays an authored episode of ordinary working language with every store-bearing utterance removed; the durable state it leaves is diffed against the state an expert session leaves, in three tiers — intent, outcome, transition — with the false-write dual reported from the same run) |
| f28 | collection_promotion_replay | operational | none | collection_candidate_surfaced_within_budget · ledger_state_matches_expectation (recurring studio licence events and a frequency-matched one-off twin; candidate observed before confirmation, dated state compared after it) |
| f29 | claimed_collection_routing_replay | operational | none | claimed_observation_reflected · no_structured_write_beyond_expectation (published observations with exact text and Evidence, plus tentative turns; existing claiming collection) |
| f30 | artifact_role_promotion_replay | operational | none | role_signal_delivered_after_write · role_state_settled_with_provenance (two same-topic reusable methods and an attributed synthesis; protocol-only and already-promoted twins) |
| f31 | transient_state_hygiene_replay | operational | none | transient_signal_delivered_after_write · transient_state_settled_without_dismissal (current same-episode pending/result pair; historical and different-trial twins) |

| f32 | utility_action_episode | operational | none | utility_action_state_valid · utility_no_prohibited_effects (seeded helpful-history, self-contained and stale-distractor action episodes) |
| f33 | referent_capture_replay | operational | none | referent_entity_typed · referent_key_edge_present · referent_used_in_fresh_session · twin_left_no_referent (six positives from the owner's list, each with a twin that names the same referent as often but as a passing or one-off thing; one fresh owner-anchored session per positive) |
| f34 | organisation_autonomy_replay | operational | none | key-scored outcome per §7 sequence 8; no §2 assertion (40 held-out situations: relation, referent and durability twin, placement and vocabulary freshness; each a capture session and a fresh-recall session) |

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
lifecycle_consequence_landed_unprompted
no_structured_write_beyond_expectation
collection_candidate_surfaced_within_budget
ledger_state_matches_expectation
claimed_observation_reflected
role_signal_delivered_after_write
role_state_settled_with_provenance
transient_signal_delivered_after_write
transient_state_settled_without_dismissal
utility_action_state_valid
utility_no_prohibited_effects
referent_entity_typed
referent_key_edge_present
referent_used_in_fresh_session
twin_left_no_referent
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
- **lifecycle_consequence_landed_unprompted**: the projected structured
  collections carry, for every consequence the corpus declares, an item whose
  declared natural key matches the deliverable, an event item whose declared
  natural key matches the observed outcome, and the status the outcome implies
  on the open item; OR a documented collection/query surface that resolves each
  declared consequence back to a live item carrying the same natural key and
  state. Titles match after NFKC normalisation, case folding and whitespace
  collapse and nothing looser; a date field must be present and valid on every
  event item, and its value is compared only where the utterance stated one.
  Counted per tier — intent, outcome, transition — and complete in **every** tier
  or it fails, with the fractions and the missing keys in the evidence. An empty
  or missing collections projection is an **error**, never a pass.
- **no_structured_write_beyond_expectation**: the same projection carries no
  item and no event outside the declared expectation, no item holding a status
  the expectation never assigned, no collection beyond the seeded pair, and no
  page the session wrote; OR a documented enumeration of everything the session
  wrote resolves every entry back to a declared expectation. A page counts as
  written by the session unless it is present in the projection of the seeded
  vault taken **before the first turn** — the seeded page set, recorded by path
  on the run manifest — or it is a collection's own manifest file or a file
  under the storage subdirectory that manifest declares, which the item and
  event checks already cover. Any other page under a collection's directory,
  including prose written where an event belongs, is an extra. It passes only on
  an empty extras set and lists the extras otherwise. Reported beside its
  coverage dual and from the same run, never on its own; an empty or missing
  collections projection, or a missing seeded projection, is an **error**, never
  a pass. The seeded projection must be **this phase's own**: taken in the same
  phase as the scored projection and before that phase's first turn. Reached
  snapshots accumulate across a trajectory's phases, so a phase that took no seed
  projection of its own would otherwise be scored against the previous phase's
  post-run state — under which a page both arms wrote is already in the
  "baseline". A projection from another phase, or one that already holds an event
  or an item outside the seeded pair, is an **error**, never a pass.

Claim-conditioned N/A: a product whose own materials claim a property scores
**fail**, not not_applicable, when it is absent — with the claim cited.
Any not_applicable in a family excludes that family from every comparative
claim, for all providers.

### Collection-claims replay predicates (sequence 4)

- `collection_candidate_surfaced_within_budget`: at the corpus-annotated boundary before confirmation, a projected attention or due-state signal names the domain as a collection candidate. The frequency-matched one-off twin composes `signal_absence_checked_across_all_surfaces`, including counters. Missing projection blocks; it never proves silence. A structured signal-class/target pair or a documented provider mapping into that pair satisfies the predicate.
- `ledger_state_matches_expectation`: a snapshot pair brackets the scripted confirmation. The resulting collection claims the domain and contains exactly the corpus's dated events, matching identity, date, status, amount, observation precision and source references; tentative turns add nothing. The collection must not exist before confirmation. The twin creates none throughout the episode, whose original baseline contains no collections. Collections may use item files or a documented tabular representation projected into the same declared keys and authored values.
- `claimed_observation_reflected`: every annotated publication appears once in the seeded claiming collection with the declared natural key, byte-exact final text, matching SHA-256 and every source resolving to preserved Evidence containing the complete supplied proof artifact. Immutable text/plain artifacts are rendered in full in the native user input for both arms; a filename alone is not attachment input. The representation may be item files or documented tabular rows. It is paired with `no_structured_write_beyond_expectation` from the same run and arm; that dual rejects duplicate keys, extra items, extra collections and unrepresented files, including orphans inside a storage directory. Only seeded pages, manifests, individually projected item or backing-file locators and resolved proof Evidence are permitted.

### Artifact-role and transient-state replay predicates (sequence 5)

- `role_signal_delivered_after_write`: after an evidence-bearing write, an actual client-visible tool result or response must contain an `artifact_role_promotion` review reference for each of the two distinct reusable methods and the multi-source synthesis. A projected audit row alone does not count. The protocol-only and already-promoted twins must remain quiet on every declared surface; a missing delivered-carrier witness blocks, never passes. Each positive signal is matched to its own origin and exact unit refs.
- `role_state_settled_with_provenance`: after consent to a governed extraction or a correctly scoped proposal, the two methods have distinct represented procedure or technique units in active eligible compiled pages; they may share one page. Each represents its method byte-exact after line-ending normalization and links to the required procedure and outcome units. The synthesis has an active research-note home with a distinct finding, inference, or synthesis unit representing its substantive text byte-exact and linking to its origin unit and both distinct visible source documents. The original experiment and source pages remain present with their evidence intact. The relevant review candidates clear by represented state, never by a fabricated dismissal or unrelated destination. A proposal without authority may satisfy the authority step, but cannot by itself satisfy state settlement.
- `transient_signal_delivered_after_write`: after an observed same-episode result is written, a client-visible carrier must identify the unqualified current pending assertion and result as `transient_state_review`. A stored-only projection is insufficient. Historical and different-trial twins must stay quiet; missing carrier evidence blocks.
- `transient_state_settled_without_dismissal`: a governed correction makes the pending wording historical, removes it, or explicitly supersedes that unit while retaining the observed result and earlier history. The current pending/result candidate clears by state. Historical and different-trial quiet twins preserve every authored unit, including their pending claim, and cannot settle through synthetic dismissal. A triage dismissal alone, an unrelated edit, or a result from another episode does not settle it. A false write from a tentative or elapsed-time turn fails the same arm's paired state check.

All sequence-five inputs are generic immutable conversation and artifact fixtures. Control turns preserve the negative condition: protocol-only turns add no reuse or synthesis claim; historical turns describe only an already-past pending assertion; different-trial turns locate the observed result in the other trial. Every user turn is gated against store-bearing commands at corpus construction and fixture loading; its checked-in bytes are compared with the pure corpus. The delivered-carrier witness is extracted from the native client stream, not inferred from the internal projector. Per-arm development reports show carrier and state results together without aggregating arms or twins. Until founder acknowledgment, f30–f31 cannot produce comparative runs, scores or claims; incomplete or isolation-guard-blocked runs are harness findings with no product score.

### Downstream action utility predicates (sequence 6)

- `utility_action_state_valid`: the observed final configuration equals the project-scoped procedure and constraint current at the action cutoff, and the complete accepted-write history contains no prohibited other-project modification. An explicit final write or a correct ordinary artifact retained from an earlier session may satisfy state equality. An answer string, recalled fact or capture count cannot substitute for observed state.
- `utility_no_prohibited_effects`: the complete accepted-write history contains no write to the other project. Damage remains a failure after later repair and is counted for both arms even when both fail the task. Missing action-history evidence is unsupported, never a clean pass.

f32 compares whole-system Exomem utility with a no-memory control on one seeded configuration world. Each episode has experience, change and action sessions with fresh conversational state; ordinary workspace notes and task artifacts persist identically in both arms. Exomem adds shipped guidance and public memory tools; capture and maintenance are actor-authored. Private oracle targets, seeds, evaluator identities and manifest hashes are never actor-visible. Current authoritative task facts remain recoverable by both arms. The initial variants are helpful history, a self-contained action prompt and stale/attractive history; they share action grammar and deterministic generation version. Rotate arm order by seed and variant index, without retries or extra attempts for a failed arm. Synthetic useful/harmful controls validate the instrument only.

Per variant report scheduled, attempted, valid, invalid and missing pairs; wins, losses, both-pass and both-fail; utility lift in percentage points; harm as losses divided by successful controls with numerator and denominator; unconditional losses and prohibited-effect counts per arm. A zero harm denominator is undefined. Preserve phase-level spend, input/output/cache/reasoning usage, wall/model/tool time, turns, peak context and cumulative delivered context for failures and invalid attempts too. Unknown metrics remain unknown and reasoning included in billed output is never added again. No weighted score or general coding/conversation claim follows from the smoke test.

Absent capture, zero retrieval hits, wrong actions, refusal, product-tool errors and exhaustion of the declared actor model/tool/context/time allowance are valid task failures while the environment remains evaluable. Independently evidenced credential/transport/identity faults, bad fixtures or broken isolation are infrastructure invalidity. This f32 taxonomy does not change another family's denominator rules. Unknown spend remains reserved and stops further calls. No diagnostic integrity score suppresses a valid f32 loss.

The first paid smoke is one development seed (11), three variants, two arms and three sessions: six episodes and eighteen fresh sessions. Freeze generation version and all input/oracle/tool/guidance/config hashes before execution. Paid execution is opt-in with a hard $2 total cap, eight model calls per phase, 48,000 input and 2,000 billed output tokens per call, 64 tools per phase, 180 seconds per phase, and no retries. Pin model/provider/effort/tokenizer/pricing, disable fallback and verify the endpoint ceilings at $0.15/M input and $0.50/M output. Reserve complete pairs ($0.3936 conservative model maximum each) before either arm; all three reservations must fit the cap at preflight, including any other metered operation. Preserve partial coverage if execution fails. No model judge or LME replay is part of f32. Execution and comparative report loading require the acknowledged f32 receipt; development instrument tests are not comparative runs.

### Referent-capture predicates (sequence 7)

- `referent_entity_typed`: the referent is an entity page: the seeded page for the sparse-existing case, otherwise a page whose title or filename states the key's referent name under the single matching rule. Its type fits the key when the type or a registry parent in its lineage is a type the key accepts. Where the key also allows a vault-declared type, because the core pack types no such thing or because ruling R1 makes a parentless type first-class, the type also fits when the vault's own registry declares it and its lineage holds no core type. An untyped page and an unregistered type never fit. A snapshot taken without the entity-graph projection is `unsupported`.
- `referent_key_edge_present`: a typed edge joins the key's person page and a referent entity, in either direction, and the vault's relation registry declares its relation in a family that the key accepts. A generic edge, an edge of another family or an unregistered relation fails, and the evidence lists the joining edges.
- `referent_used_in_fresh_session`: the activation packet that the fresh session receives, from its `activate_context` call or its hook, must serve the referent entity's ref (its path or its identity); otherwise the assertion fails. It passes when the packet serves the ref and the fresh answer contains the key's exact value under the single matching rule. When the packet serves the ref and the answer lacks the exact value, the result is `unsupported` with a `judge_fallback` evidence prefix, and only the judge fallback of §7 sequence 7 decides it. A missing packet or answer capture blocks.
- `twin_left_no_referent`: against this arm's seeded snapshot, the run left no entity, edge or notice for the twin's referent. It added no entity page for the referent, and no edge of any kind (typed, neutral-predicate or generic) whose endpoint is such a page or names the referent. A notice is a promotion-class or entity-class signal on any declared surface whose target names the referent or is such a page. The predicate composes `signal_absence_checked_across_all_surfaces` with the referent name as its subject, so an unprojected surface blocks and never passes.

All sequence-seven inputs are generic and invented: an authored seeded vault, twelve case files and an answer key, each frozen by SHA-256 in §7. Every user turn and every fresh-session question passes the f28 store-bearing gate at case-set construction and at scenario load. Entity types and typed edges come from the opt-in entity-graph projection (projector 0.7.0), which reads the vault's own entity-type and relation registries. Earlier families' snapshots keep their content but not their bytes: each now carries `typed_relations: []` and projector version 0.7.0.

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

- 2026-08-23 — **Lifecycle-routing replay family amendment (sequence 3).** Reason: the lifecycle-routing contract slice shipped the runtime for routing lifecycle consequences and an acceptance journey that proves it *given the expert end-state* — the journey drives the library writes itself. Nothing in f01–f26 measures the north star that work was undertaken for: a real agent, on the shipped contract, given ordinary work language with every store-bearing utterance removed, produces the same durable state the expert session produced. f26 drives the real client envelope with no agent, track C drives single fresh sessions and measures retrieval activation, and f16 measures plan-versus-record divergence over authored state; none runs a multi-turn agent through a lifecycle episode and diffs what it wrote against what an expert would have written. Contracts-first is the house rule sequences 1 and 2 established: the measurement files before anyone tunes the contract against an improvised test.

  This amendment extends §1 with f27 `lifecycle_routing_replay` (operational journey; no public coverage; an authored, ordered transcript of user turns in ordinary working language is replayed through a real agent CLI against an isolated server over a seeded vault, and the durable state it leaves is diffed against the fold of the corpus's own per-turn annotations in three tiers — `intent`, a plan item filed from stated intent; `outcome`, a record appended from an observed event; `transition`, an open item whose status an outcome moved). It appends two deterministic assertions to §2 and adds their representation-neutral predicates to §4. It adds **no** operation kind: the trajectory uses `configure` for the arm, `agent_turn` per user utterance and `snapshot` per arm, all already registered. `UNPROMPTED_FAMILIES`, the absence-meta composition set and the item-pair set are unchanged — the user turns are the stimulus, and both assertions are state assertions rather than signal-absence assertions.

  **The expectation is authored, never produced by an agent.** The corpus is a pure function of its arguments — a seeded vault, the ordered transcript, and per-turn annotations — and the expected end-state is the fold of those annotations. Turns that land nothing are in the corpus on purpose: at least one tentative claim, one elapsed-time remark and one deferral each annotate `none`, because the shipped contract says a tentative claim is never an event and elapsed time is never an outcome. A family with only positive turns would score a product that wrote something for every utterance as perfect. Deliverables are referenced by their seeded titles and matched after NFKC normalisation, case folding and whitespace collapse; a miss is a miss, and loosening that is a new amendment.

  **The stimulus is clean by construction.** The corpus module pins a store-bearing vocabulary — the store itself, and the act of storing — and refuses any user turn matching it, at corpus construction and again at scenario load, naming the turn and the matched token. A red fixture carrying "save this one" proves the refusal. The retrieve-nudge's own activation regex is the sibling and is deliberately **not** reused: it matches `earlier`, `previous`, `history` and `decision`, which are exactly the ordinary working language this family measures.

  **A harness fault is never a product result.** A non-zero exit, an error-subtype or `is_error` result, a login failure or a malformed transcript line marks the arm a harness fault: no snapshot is produced for it, both of its assertions evaluate `blocked` with the reason, and nothing about the product is scored from it. The same rule covers an unprojected collections section — an observation error is not a finding about the runtime.

  **Paired readouts, no aggregate.** Coverage is counted per tier and reported beside the extras count from the same run, together with the capture-nudge firing count and the structured-write tool-use count, under a manifest pinning the client version, the model, the product version, the prominence level per arm, and the corpus and fixture digests. No figure aggregates across tiers or across arms, and a coverage figure is never published without its false-write dual. The house rule from sequence 2 applies unchanged.

  **Expected partial.** On the runtime that exists today the family is **expected partial**: the outcome and transition tiers are the ones the contract slice taught, while the intent tier depends on the agent filing a plan item from stated intent without being asked, and the hookless arm has only the tool surface and a pasted instruction block to teach it. That is the family's finding and its falsification target, not a CI failure. The next contract slice is built against this family, and "became more automatic" means its tiers flip green while the extras stay at zero.

  **Catastrophic set: no additions.** A missed consequence is a trust failure, not an integrity failure; putting one in §3 would suppress every aggregate over a routing miss. This amendment adds **no** catastrophic assertions and no budget constant — the tier counts are fixed by the corpus, so there is nothing to calibrate and nothing to freeze. It changes no projector, no runtime and no tool surface, and performs no witness join against the server write log: the projected vault is the state witness, and the transcript's tool-use counts are reported beside it rather than reconciled against it.

  Until the receipt is acknowledged, f27 MUST NOT support a comparative run, score, or claim.

- 2026-09-12 — **Collection-claims replay amendment, sequence 4.** Add f28 `collection_promotion_replay` and f29 `claimed_collection_routing_replay` to §1, their three assertions to §2, and their representation-neutral predicates to §4. Both are operational journeys in two client arms, with ordinary user turns that refuse the existing store-bearing vocabulary plus collection, ledger, schema, claim and tracking terms at construction and load. The f28 positive observes candidacy before its scripted confirmation; its frequency-matched twin must stay quiet on every surface. The f29 positive is inseparable from the existing false-write dual. Assertions read the fold of immutable corpus annotations and projected state, never agent claims. Add no operation kind, catastrophic assertion, or budget constant. The existing audit, attention and due-state projections gain candidate domain/evidence metadata; structured projections gain default-empty authored claims and field values plus item locators; page projections retain declared stable identities so exact text, provenance and unrepresented files can be checked (projector 0.5.0 → 0.6.0). Evidence projections also retain the unnormalized source body and independently hash referenced artifact bytes; canonical non-Markdown Evidence and Records files are inventoried so orphan files remain observable. Existing projected fields retain their meaning. Until the receipt is acknowledged, f28 and f29 MUST NOT support comparative runs, scores or claims before founder acknowledgment. Development artifacts are findings about the harness and runtime only, and a harness fault blocks an arm rather than scoring it. Every executed turn requires a successful terminal client result; empty or initialization-only output never counts as completion.

- 2026-09-12 — **Artifact-role and transient-state replay amendment, sequence 5.** Add f30 `artifact_role_promotion_replay` and f31 `transient_state_hygiene_replay` to §1, their four paired assertions to §2, and the delivered-carrier and state-settlement predicates to §4. Reason: existing topic and collection journeys cannot distinguish same-topic artifact role from a protocol, or current pending wording from historical and unrelated trial language. The generic immutable inputs cover two reusable methods, a synthesis attributed to two sources, a protocol-only twin, an already-promoted twin, a current pending/result pair, and historical and different-trial twins. Every user turn remains free of store-bearing commands and is checked at construction and load. The delivered witness is a successful native client response after an evidence-bearing write; an internal projection cannot substitute. Resolution requires governed authority, exact represented unit and source provenance, retained origin history, and state change rather than synthetic dismissal. Each arm reports its positive and quiet dual separately. No operation kind, catastrophic assertion, or budget constant changes. Until founder acknowledgment, f30 and f31 MUST NOT support comparative runs, scores or claims. Guard refusals and incomplete native sessions are harness findings with no product score.

- 2026-09-13 — **Downstream action utility amendment, sequence 6.** Add f32 `utility_action_episode` to §1, its two state/action assertions to §2 and its controls, accounting, validity taxonomy and bounded pilot protocol to §4. This separates downstream utility from retrieval diagnostics, retains negative transfer and damage, and meters the full actor-authored lifecycle. No catastrophic assertion, operation kind or other-family budget/denominator changes. Founder acknowledgment releases only f32; earlier pending families remain withheld.

- 2026-10-10 — **Referent-capture replay amendment, sequence 7.** Add f33 `referent_capture_replay` to §1, its four deterministic assertions to §2 and their predicates to §4. Reason: the `promote-referents-without-nudges` change claims that an agent promotes the durable referents of ordinary conversation into typed entities and edges without being asked, and that a fresh session then activates them from the person. No family measures that: f21 measures candidacy from recurrence, and f27 to f31 measure lifecycle, collection and review routing. A case-authoring lane files this entry and authors the case set and its answer key from source truth, never from a measured session. Implementation lanes never read the cases. A measurement lane, never an implementation lane, runs every arm.

  **Cases.** The case files live in `benchmarks/epistemic/fixtures/sequence7`. The six positives cover the owner's list: pet and owner, person and kin, equipment and its consumable, supplier and operator, community and membership, and a sparse existing entity that the seeded vault already holds and the turns enrich. Each positive has one negative twin that differs by durability, not by mention count: the twin names the same referent as often, in the same number of turns, but as a passing or one-off thing. Each positive has one fresh-session question that names the person and asks about the referent. The answer key states each answer fact as one exact value (a name, a number or a date), and every key value is stated in the authored turns. The turns are ordinary working language. The f28 store-bearing gate, which the scenario loader applies to f33, refuses any turn or question that names the store or the act of storing, at case-set construction and at scenario load; this entry adds no word list. The seeded vault is generic and invented. No case shares a name or wording with the examples in the change's artifacts, nor a type pair or relation pair with the CI replay of the change's task 3.2.

  **Pass rule per repeat.** A positive passes a repeat when all three hold: the referent entity exists with a type that fits the key (`referent_entity_typed`), the key edge exists (`referent_key_edge_present`), and the fresh-session answer uses them (`referent_used_in_fresh_session`). "Uses" is deterministic where it can be. It fails when the fresh session's activation packet does not serve the entity's ref. It passes when the packet serves the ref and the answer contains the key's exact value. A twin passes a repeat when no entity, edge or notice exists for its name (`twin_left_no_referent`).

  **Judge fallback.** The fallback covers one case only: the packet serves the ref, but the answer lacks the key's exact value. Then a blind judge from another model family than the model under test decides whether the answer states the key fact in other words. The judge sees the same reference view for every arm: the question, the key's referent and exact value, and the answer, under an opaque item id, with the arm key withheld. A judge never overturns a deterministic pass or fail.

  **Pass line (ruling R6).** The owner delegate set this line on 2026-10-10 under the owner's standing authority. It is not the owner's own ruling, and the founder acknowledges it with this amendment. The shipped arm meets the line when all three hold:
  - the pet-and-owner positive passes in both client shapes;
  - at least 5 of the 6 positives pass, counted in each client shape;
  - no twin repeat is a false positive, in either client shape.

  A positive case passes when it passes 2 of its 3 repeats. A twin case passes only when all of its repeats pass, so one false-positive repeat on any twin misses the line. A blocked repeat is rerun under the same pins until the case has three scored repeats, with at most six attempts per case per client shape. A case without three scored repeats is `incomplete`, and the pass line cannot be met while any case is incomplete. An `incomplete` case counts as not passed in every rule: the pass line, the beats rule and the keep rule. The owner delegate set this rerun rule on 2026-10-10, and the founder confirms it with this amendment.

  **Runs and arms.** Every arm runs through the f27 driver, which the measurement lane extends before the baseline with a second, fresh `claude -p` session per run for the owner-anchored question; the run file records the activation packet that this session receives. Each arm runs hookless and hooked, 3 repeats per case per client shape, on the same authored turns. The case files fix the prominence level per client shape: `maximal` hookless and `balanced` hooked. The run manifest pins the client version, the model, the product version, the prominence level and the digests below, and every arm run checks the digests before its first turn. The arms are:
  - baseline: `main` at the base revision;
  - arm B1: the authority text and the typed-edge leaves (groups 1 and 2 of the change);
  - arm B2: arm B1 plus the receipt block (group 3);
  - arm C: the shipped B arm plus `referents` (group 5), run only on escalation, with the shipped B arm run again beside it under the same pins.

  The hookless arm is a proxy for a hookless client such as ChatGPT, never a ChatGPT result.

  **Kin case on the baseline.** The kin positive's key edge needs the kinship family, which arrives with the change's task 2.2, so the baseline cannot reach it. A flip on that case from the baseline to an arm is therefore not a behaviour gain. No decision rule compares an arm with the baseline: the keep rule compares B2 with B1, arm C is compared with the B arm run beside it, and the pass line applies to the shipped arm alone.

  **Beats rule.** Arm X beats arm Y when X passes at least one positive case that Y fails, fails no case that Y passes, and has no false-positive twin repeat in either client shape.

  **Keep rule for the receipt block.** The block ships, and B2 is the shipped arm, only when B2 beats B1, or when the run files show the agent acting on a listed name through its route and B2 fails no case that B1 passes. Otherwise B1 is the shipped arm. For B2 the report gives the block emissions per write (the share of writes that carry a block, and names per block) and the shares of listed names acted on, dismissed and left alone.

  **Escalation order.** When the shipped arm misses the pass line, the change stays open, and the misses decide the next mechanism. An activation miss is a positive whose entity and edge exist but whose fresh-session answer does not use them. When every miss in the shipped arm is an activation miss, decision 4 (typed activation) runs first, then decision 1B (`referents`). Otherwise decision 1B runs first: arm C is built and run, and it becomes the shipped arm only when it beats the B arm run beside it; decision 4 follows when its trigger holds. The D4 trigger is at least one owner-anchored question that fails in the shipped arm while its entity and edge exist. When the line is still missed after both, a report with the per-case tables goes to the owner.

  **Harness faults.** A non-zero exit, an error result, a login failure, a malformed transcript line, a missing packet or answer capture, or a digest mismatch blocks the run. A blocked run is recorded as blocked, with its reason, and is never scored. It is rerun under the rerun rule of the pass line, and a case that uses up its six attempts without three scored repeats is `incomplete`.

  **Frozen inputs.** The receipt records the same digests in its `fixture_sha256` field, and loading a case, the key or the seed refuses bytes that differ:
  - `f33-answer-key.v1.json`: `90cf2a482f510957bff03846ca44821c5f4f06e2ba4378dcb6efd7a54f7e2e83`
  - `f33-community-membership-twin-v1.yaml`: `0fb08981a618edfa88f1bda857508ad0fe7ea28c25c808ff2dc44c97d5b11c0d`
  - `f33-community-membership-v1.yaml`: `130c2d770cb5bcac9d0e4069b3b0e04cc9121a88a57d91ff1f5b7275873cac68`
  - `f33-equipment-consumable-twin-v1.yaml`: `613fcb6ba2e9700293bc9e289ffded14894a8c6a378b7882f5c1dcf932943330`
  - `f33-equipment-consumable-v1.yaml`: `d58086177e45627ab1e335bf5f2fa0784d61eaebd73fd91a043c9dc851af540a`
  - `f33-person-kin-twin-v1.yaml`: `422c8c8fe76fc1a5762d1f74cd338947dcef20c00f5059df41165c6edcee9b78`
  - `f33-person-kin-v1.yaml`: `00efb8583bd9a4d946e2564af9871d150d30964edd68666f0e7448c4a5a6a6ad`
  - `f33-pet-owner-twin-v1.yaml`: `7e9f9a627ee6f99ccbb5792441605406f5cc1fbf9050999307655c9f0958bebc`
  - `f33-pet-owner-v1.yaml`: `aa4ec43ce14b528f45611515caa0e80e23e60f4d0788cd243a6bf00d6fd6b400`
  - `f33-seed-vault.v1.json`: `8e36cfc3229456f81e46d03614ba981d59b659b528bfd71325ae029acbcb5320`
  - `f33-sparse-existing-twin-v1.yaml`: `cec9a25a39d0f5a73de24990d1c1e97464042ae9b1f8f6b4f9a2bb9d5863cbd3`
  - `f33-sparse-existing-v1.yaml`: `74b83acbb9311ce15c349948bc06d87f7a487a7f757d945a40b56edbfa4dc383`
  - `f33-supplier-operator-twin-v1.yaml`: `101cc725d4cbc9d243f2e932c5d24a4670ae17005d60e2b0215901f3666e40e2`
  - `f33-supplier-operator-v1.yaml`: `0074bb2fd5b1efa3818c5b5c1db0489e8a0b7673d0bcb8fa92e30fe3c6e08087`

  **No other change.** This amendment adds no operation kind, no catastrophic assertion and no budget constant. The twin's assertion composes the existing absence meta-predicate. The projector gains the opt-in entity-graph projection (0.6.0 → 0.7.0), and the receipt schema gains the optional `fixture_sha256` field. The content that earlier families read does not change, but their snapshot bytes do (`typed_relations: []`, projector 0.7.0).

  The owner is asked to acknowledge this amendment on the day it lands. Until the receipt is acknowledged, f33 MUST NOT support a comparative run, score, or claim, and every comparative result of the change, including the gate of its task 5.2, reads as pending.

- 2026-10-30 — **Organisation autonomy amendment, sequence 8.** Add f34 `organisation_autonomy_replay` to §1. Reason: the owner approved the autonomous organisation plan on 2026-10-26, and its decision 8 replaces the acceptance test of `promote-referents-without-nudges`. That change's PR B changes authoring behaviour: specific relations, referent promotion, placement and vocabulary freshness. This family decides whether PR B merges. It supersedes f33 for that acceptance only; f33 stays filed, sealed and unrun.

  **Authorship and custody.** An independent case-authoring evaluator lane wrote the source truth, situations and keys on 2026-10-30, before PR B is built. All of it is invented and product-shaped. No personal record, private incident, other benchmark case set, f33 material or system output was a source. Implementation lanes never read the files. The files stay in evaluator custody outside the repository, and this entry freezes them by SHA-256. A measurement lane, never an implementation lane, runs every arm and checks every file against these digests before its first session.

  **Situations.** The held-out set has 40 independent situations, each with its own source truth, seed vault and names:
  - 16 relation situations: 10 where a specific relation fits, and 6 where a generic edge or no edge is correct;
  - 12 referent situations: 6 positives and 6 durability twins;
  - 6 placement situations;
  - 6 vocabulary-freshness situations, where a later session meets an entry that an earlier agent session promoted.

  Each situation has one client shape, fixed before generation. Each shape gets 8 relation, 6 referent, 3 placement and 3 freshness situations, so 20 run hooked and 20 hookless. The hookless shape is a proxy for hookless clients, never a result for another client. Each durability twin copies the turn and mention structure of one positive, but shares no source truth with it. Every user turn and question passes the f28 store-bearing gate. A separate pool of 20 development situations serves the development rounds. It shares no source truth, name, referent or seed with the held-out set, and the decision never uses it.

  **Sampling unit.** The unit is the situation. Situations share one author, the common product base, the actor model and the client version; nothing sampled upstream is reused across situations. The interval treats situations as independent paired units.

  **Arms.** Both arms run the same base revision, bound before generation:
  - baseline: `main` with organisation PR A, the source-quality repair released in 0.147.0 (#2136) and the common tool catalogue (#2139);
  - candidate: the same base plus the complete PR B batch, and nothing else.

  Before generation, the measurement lane binds the exact revisions, wheel digests, interpreter, loaded source identity, served tool-list and instruction digests, contract version, engagement, authority, prominence, client version and model for each arm. A product label does not prove which code executed. Prominence is the shipped default for each client shape.

  **Runs.** Each arm runs every situation once: a capture session with the situation's turns, then a fresh-recall session on the vault it left, with one question. That is 160 actor sessions, with no repeat draws. The actor is `claude -p` with the default Opus model on the Claude subscription. A model-free script builds each seed through the arm's own product doors. A freshness seed saves its promoted entry through the governed registry door under a separate prior agent session. The fresh session follows the shipped default maps workflow through the public tool names that discovery returns. The run file keeps the full transcript of both sessions, including map, profile and page pulls, every call with its result, the final answer and the end state. An empty legacy activation field is never a failure and never forces an anchor fallback. A guard of 30 calls per user turn applies equally to both arms; its refusals are reported.

  **Measures.** An arm succeeds in a situation when three things hold: the category's state outcome, an answer that states the key fact, and no false precision. The state outcomes are:
  - specific relation: an edge between the key's pair whose meaning the source supports;
  - generic or no edge: none is required;
  - referent positive: an entity for the referent whose type is not one the key rules out;
  - durability twin: none is required;
  - placement: a page from the capture session in the expected home, or outside every unrelated home when no home applies;
  - freshness: the referent's type or the pair's edge uses the promoted entry.

  False precision is a typed claim from the capture session that the source does not support, over the referents and pairs the key names. Examples are an unsupported or reversed specific edge, a ruled-out type, an entity for a twin's one-off referent, and a page in an unrelated home. Generic edges and reference edges (`links_to`, `mentions`, `about_entity`, `cites`) are never false precision. The keys accept every equivalent grounded outcome. They prescribe no tool order, note count or wording. An answer passes when an accepted value appears in it at token boundaries after normalization. The run also reports, per arm and as paired differences:
  - omissions by reason code;
  - generic edges with and without a recorded edge-review reason, and exactly duplicated reasons after normalization;
  - abandoned writes: refused or failed writes that no later write in the session completes;
  - Exomem calls per session, and the bytes re-sent after a refusal;
  - the fresh session's map, profile and page pulls.

  **Judging.** Scoring is deterministic wherever it can be. A blind judge sees only residual cases: the state outcome holds and the session received the fact, but the answer lacks every accepted value. The judge decides whether the answer states the key fact in other words. Each item shows the same reference view for both arms: an opaque id, the question, the key fact, the accepted values and the answer. The judge runs on the Codex subscription, another family than the actor, in at most two lanes without the arm key. When Codex has no room, a fresh Opus lane judges, and the report names it as same-family. A judge never overturns a deterministic pass or fail.

  **Statistics.** Each scored situation gives one row: baseline and candidate success, and baseline and candidate false precision. The success table counts a (both succeed), b (only the candidate succeeds), c (only the baseline succeeds) and d (both fail), with n = a + b + c + d. The primary result is the paired difference (b - c)/n with its 95% interval by Newcombe's method 10. This is the hybrid score interval for paired proportions without continuity correction (Statistics in Medicine 1998;17:2635-2650):
  1. Set p1 = (a + b)/n for the candidate and p2 = (a + c)/n for the baseline.
  2. Compute the 95% Wilson score interval (l1, u1) for p1 and (l2, u2) for p2, with z = 1.959964.
  3. Set phi = (ad - bc)/sqrt((a + b)(c + d)(a + c)(b + d)), or 0 when that product is 0.
  4. Set L = (p1 - p2) - sqrt((p1 - l1)^2 - 2 phi (p1 - l1)(u2 - p2) + (u2 - p2)^2).
  5. Set U = (p1 - p2) + sqrt((u1 - p1)^2 - 2 phi (u1 - p1)(p2 - l2) + (p2 - l2)^2).

  The grouped sign-flip interval of the switch-evaluation statistics module is a different method, and this family does not use it. Per client shape and per category, the report gives the same table and interval as description only.

  **Decision.** PR B merges only when both rules hold:
  - the lower bound L is above 0;
  - the candidate has false precision in at most 1 more situation than the baseline.

  An interval that contains 0 establishes no gain. PR B then does not merge, the per-situation tables go to the owner for a ruling, and the batch is not expanded after the result.

  **Faults.** The run reuses the maintained switch-evaluation fault classification. A harness fault is retried with the same inputs at most twice; a retry is not a repeat. When either arm's session still faults, the situation leaves both arms and the report lists it. More than two struck situations void the run, and a rerun needs a new amendment. A product failure after readiness is scored as a failed situation. A digest mismatch stops the run before its first session.

  **Spend and holds.** The decision run uses 160 actor sessions; at most 80 development sessions precede it. Residual judging uses the Codex subscription. No API or credit spend is authorized. The decision run waits for:
  - PR A's release and the bound base revision;
  - the common tool catalogue served by both arms;
  - an isolated write-capable product binding on the maintained Compiler capture route, agreed with its owner (that route exposes four read tools today);
  - the owner's acknowledgment of this receipt;
  - the session manager's actor allocation and judge room.

  **Frozen inputs.** The protocol file `protocol.yaml` has SHA-256 `dea081ca7f25bee772a852fe4cf241adce5745a74aaafd2fac4c2e37dce050e1`. The held-out case files `h01.yaml` to `h40.yaml` and key files `k01.yaml` to `k40.yaml` have these digests:
  - `h01.yaml`: `c43d230752c360b1adba5bcf5cdb190557677b31924ae475b01829c50ba575e2`
  - `h02.yaml`: `0d9ab047827199eadb0da2e207a17f433c553e58c61bbb2dbf2ea47020d63737`
  - `h03.yaml`: `bbe723f86753f72e6e118bbdbcc6dd78fe2ac971cc52c533c0a63f0c60442915`
  - `h04.yaml`: `dac44134fc2bc9df053ad32edf67d1982b8e48a6635ff15c5c624427d05b9201`
  - `h05.yaml`: `79c2df188ecad3484a35be17a361b59c6440cd0a3964984f70a487c73678cdfb`
  - `h06.yaml`: `9b44b69e4e77a5df4fdecdc8b856b1c644e0fc73dfb3afc59934cb64528781d0`
  - `h07.yaml`: `127de988163ad2e9bd7c3bfc9258bc7ee1a8f74f6e03a94aa9e4663c05c433cd`
  - `h08.yaml`: `6900f8fc23a593b0a5721c1d28299ce2f74b0786bbe795067a047569e642a03d`
  - `h09.yaml`: `8f5ccc84f45380eeffac1e079533a0d3acfbe430fc5c0e6b2e7ed53e9591e86f`
  - `h10.yaml`: `fae88edc3c58818ccbd7b57c6bbdcd49956d92f9671d1d8e9ca8bbda28fd9019`
  - `h11.yaml`: `54562a4b1f9aae3e21d5b45e82df56e7334751d7064ecf21769a5dd551c59fa3`
  - `h12.yaml`: `e3f2eb4331743e07b6327985bc4d8ad044885264a0fa64cb236c214725ae0311`
  - `h13.yaml`: `ff1b78c720a984eec388b99af67556d0e08b9f1521c34696bc2163cb7858a579`
  - `h14.yaml`: `9b8d0ae65f57bcf72ba8322f2abe97213c4079c83d964ec0fc25da31d31c9758`
  - `h15.yaml`: `74fc50e22ed14a5847896d83c67fc8037d6c4fbb04287f8d3feaebc9147e82a2`
  - `h16.yaml`: `da00d7e5faa988add481809e62d3190441c9d8d0b1ca09b2c70ef9fafbf1b250`
  - `h17.yaml`: `c47c2155abb7409c9a768cf98992fbf0c615bb2c2cde3c5edcd1cdb86e93086d`
  - `h18.yaml`: `4270433b6433d28ba2e77cb5974ef5d9fa3f169cdbf1b2446620e2f3e8f9eaf5`
  - `h19.yaml`: `c13814f6d4c47344a8828d4a11222aef80d317528ce3dd56aed32e20eef2a71a`
  - `h20.yaml`: `32558877c66ea5fb65e4af0c5642786ec8821a820186b797a3d8edb2889a945c`
  - `h21.yaml`: `f4032b9d147f0c977f6594e99d8a8a081999093e9ba225b74c2faec19f02f57f`
  - `h22.yaml`: `026af3f7700004f3de850432a793bd13fdc982b1c373a4bd6f760fd366b0e3af`
  - `h23.yaml`: `6fe50dd478fb528b010903271336bc9bd9a1c880f6467285c9d62b85859edc7a`
  - `h24.yaml`: `b0887a4352594a8bc186d417daff8857be11409ab254860692a0019eb2789729`
  - `h25.yaml`: `a6df9766e64d8aca199d7c416a07cd0ab340e0797f0ea9d0428a69eef9537a21`
  - `h26.yaml`: `ee58674ddc7185c09f2e14b17451c59a24a45a7030c026d254931c1b6edabf84`
  - `h27.yaml`: `3e5dc03cdc00e7bb985cc576c43c5aca76a393320c11a28314363113a9313b58`
  - `h28.yaml`: `025df219018903bafa48e5defa64a94ffff9816e164d9dd384ac1778566ac974`
  - `h29.yaml`: `2abd16d0b0dafe06f3f2b5011eb52de7f5436dd20a624941c92dabbc09cbd765`
  - `h30.yaml`: `a8a60957a35874f8647b7ad7ee3a0482c605b5d94436a18a2d55f622d6f289f3`
  - `h31.yaml`: `825788032af53075adef9ba4422ebc3727a1617f188405c1bc4b9535ef36d6aa`
  - `h32.yaml`: `92824d8ed6c11102701c25e3c5b593bed8870f7a15ebb2dd2a72cbc95556a34e`
  - `h33.yaml`: `3b7e09eef83ec1c13edb91d6f284140e8482dddb27cffb6338dbd780821f1fe0`
  - `h34.yaml`: `ecca38b358035791c907cdbb45613125ae8cc7959481c8f077b96c293c88ecbd`
  - `h35.yaml`: `442783c3845411ab80c053034dcb07a39743a557c2023f4d44385ea50cc81263`
  - `h36.yaml`: `be8e7ed6d7a4fcb7f3967ececd047794330628b29519ab4cf0f2a24fdb8a9d63`
  - `h37.yaml`: `a5fc5b0ea432200cdbd111e93251aecbc9de82ca718bfc71ad9eee255e9d3caa`
  - `h38.yaml`: `5eeb1ff163e1208ecd04b0cf79a3f153b39787e57c0fcd39a0d1e1426436de3f`
  - `h39.yaml`: `a54bfeac56f3abe246e8868b65e7571c28dc6d9415556b2249f5a66a23fdcc87`
  - `h40.yaml`: `67b8610f6a1cb7407ae0fe6bd55445c540a6237c18d2f5fb926e0e196fe9ead7`
  - `k01.yaml`: `5a7ff8fdce31c4f29c2970620d5d22c7df4035284023b794c51948c813986e4b`
  - `k02.yaml`: `95502f62a6268bb42dfaccb892b0ad0e3060ac704ff79f8289d2373aa8f8b73c`
  - `k03.yaml`: `048f35cc96e2c652e9e8d703c118a06724c2ec32f69b6eb9a14dd01c40ef7e2d`
  - `k04.yaml`: `0029876178914c3eab1ade3f44dc4835bfa234edb09779ca5aba61023e68c045`
  - `k05.yaml`: `58c8fe089aab2c7b716015814995b52ce0796e02f7200f76253d9df95f8425f9`
  - `k06.yaml`: `67049f765bec89447d0e00ed0507f0749d675bbebe99ca8fb5586e426df88505`
  - `k07.yaml`: `0055d2811c7a40e6d1cff25607d826eeb126193dd6d6db7670930c1c8c22f201`
  - `k08.yaml`: `9691285c8a608d8e5bc0aee97ae8641463496f86cdffaf34b06329fc11734c99`
  - `k09.yaml`: `6bbe0e91b4c688f22060f15f0f77fd78976bbefacb8c01c8a00a7cea0d422183`
  - `k10.yaml`: `31f397bbc3316fd0a1f6f6beae4db3fdebf49144ed6409e22c04d483ad3825ba`
  - `k11.yaml`: `fb1917a2ded76135442e1b83f8bbfc093d8a80c0a126e790a85942c6742779d6`
  - `k12.yaml`: `8b7d5639ffe0692d4720f327f0a9a2612b79fa4043af4871a2059d4e2d0ab0d8`
  - `k13.yaml`: `bc036ab7ca65ff61dc408a5e2ae61dc83d2e787cbf84ba9995e23ed06cc9232d`
  - `k14.yaml`: `2eb839a242291941c6afdf2735c7bff618b30d2317fadb551982ee4e5b9fae80`
  - `k15.yaml`: `b13c696a5451bff35a4e72e6e77f44d50dc4e1ae6864cbaeb58bcc2099715966`
  - `k16.yaml`: `a1523737cb450a957b3680786bc67101a61a5ff69d6355f117ec830eb0a42453`
  - `k17.yaml`: `7fa4c2b8d69b4a01b9768806c671ba37a130e443a49eea5ff5235f742c506836`
  - `k18.yaml`: `3bc4e922cc8c743df2999a30236acc7b8fa84e5589f7fbbedd5a82c367f14411`
  - `k19.yaml`: `82fc93d733dce620525bfdbe3a6cdc17513ccf273cd55bff5c7f90cc2cf274d1`
  - `k20.yaml`: `f03e506c45ebbf98c0095a4d2ac99d3eb4ccf0bb398c3413da9173f10fd864a6`
  - `k21.yaml`: `beed1f675f4197073e2d05b67ac4ed32354ab2cad9d02f37c8ecdde1b3ab6b83`
  - `k22.yaml`: `0b80bc8a54cd70af0d3491cf09e98ebda2f09db7f86097240ce2dce95ebdb062`
  - `k23.yaml`: `c5793400cdd6f094af24ded8e6dc811d66f05efcbd807d4672d6a74a2a2f69ca`
  - `k24.yaml`: `6e7ed11758907f3a5e757bb33c4192a14970e35526e8d225e3373efbe4085eb2`
  - `k25.yaml`: `d38e8f0b6c9327818e40390ec8f1dfab9d951958b23b1babe38f611cc92e9e70`
  - `k26.yaml`: `7c831ccbfa21a0b50c43e5e3eb7d1f20b7b13aaca45f24b85a98fd2b1877b1ee`
  - `k27.yaml`: `9472e10fa267b953970347fced88c2fcd815a9a95273240e36064e12ef39705a`
  - `k28.yaml`: `a479aaf9879c1aad89ac2eddb1860bf6910bdfb6d55b7bb722ad03449080620a`
  - `k29.yaml`: `0b0709860ed3190f7757ea87d639bfbf96f666ac75e7a9b0c48536ce4f33a8d4`
  - `k30.yaml`: `048980ac6f5ebfd0dd8e7bfcd799139f470446c70ac927c7c4321a9324463978`
  - `k31.yaml`: `15274c8cffdd078c8cbc3dec3cfddb67a5164f23874089b94c2186cb7c575d3f`
  - `k32.yaml`: `7cbdad5bd2001b8bc404e3d77250385e19229e64a6f01770736bf1b1bb07f60f`
  - `k33.yaml`: `eebd42dda955c6a9825b2b74ab00b0499745cef57729a3b6f4b752c2c8d0618c`
  - `k34.yaml`: `525d362fa1aa31a5eba25b4249ce209120ba8073b6f9cef632c44b7d219ba2de`
  - `k35.yaml`: `56d2f77a3445e964443be2975aa8daa700c218b55f3e50c4e94c1816f0a76cdf`
  - `k36.yaml`: `17411d38e3938678a6622ec43a7dbca0c5e6c87dd07f72e737c6b3ba9f1d45f8`
  - `k37.yaml`: `481c829492e88f9f34b90e4766907ba7dfb8d8bb451be74d471310e860ca5141`
  - `k38.yaml`: `d3e758114dedfb89fb480e1481c22fe2676834f4d30f8d4e3c9b222845f90505`
  - `k39.yaml`: `2df681fea732bfe7b10a52875572d8ce8aa8eda9bfd213346af2a25f07d8d096`
  - `k40.yaml`: `ed84387669b8e2a82fd771664dd72aa29426b9d3fee983f301e1e7d112b2f6ea`

  The aggregate digest is `2feb75c61589b5dacad5b2ef96a557118663141e4e690fc7912fedadfe97ae01`: the SHA-256 of a text with one line per file above, in name order, each line being the digest, two spaces, the file name and a newline.

  **No other change.** This amendment adds no §2 assertion, operation kind, catastrophic assertion or budget constant. The measurement lane scores f34 against the frozen keys under this entry's rules. The owner is asked to acknowledge this amendment before the decision run. Until the receipt is acknowledged, f34 MUST NOT support a comparative run, score or claim, and PR B's acceptance reads as open.
