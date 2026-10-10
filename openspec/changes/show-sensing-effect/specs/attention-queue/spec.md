## MODIFIED Requirements

### Requirement: Question aging and supersession integrity complete the due-state consumer set

The audit registry SHALL add two categories, each producing review items with the standard
reference, fingerprint, dismiss/snooze/reopen, and material-change-resurfacing semantics:

- `question_aging`: a governed question unit on an active page SHALL surface once the page's
  authored date is at least a configured age old and the unit carries no answering structure,
  reported as a review candidate, never a defect. The answering test SHALL be unit-local — no
  `verdict` on the unit and no outbound relation authored on the unit whose registry-resolved
  canonical kind is one of `supports`, `contradicts`, `resolves`, or `evidenced_by`.
  Because its age threshold is system-invented rather than authored, the category SHALL be
  registered and selectable but SHALL NOT join the default attention union.
- `supersession_integrity`: a supersession pointer (`supersedes` or `superseded_by`) whose
  target does not resolve, and a supersession chain carrying more than one current head,
  SHALL surface as defects. Because the pointer is human-authored and no threshold is
  invented, the category SHALL join the default attention union, ranked immediately after
  the queues that fire on an authored date and immediately before the queues that infer
  their own candidates: a defect in authored state outranks an inference, and is outranked
  by an obligation that expires. Parked page statuses SHALL NOT exclude a page from this
  category, because a `superseded` page is where a dangling forward pointer lives.

This change's delta to the default union is stated here rather than as a further MODIFIED
requirement against `Unified Review Surface Composed From The Epistemic Queues`, because two
unarchived changes already carry one and a third would collide at archive-sync.

The remaining two due-state categories, `prediction_window` and `unfinished_experiments`,
are owned by the `add-prediction-window-review` and `close-experiment-lifecycle` changes
respectively; this change consumes them through the projection unchanged and neither restates
nor redefines their predicates.

A review reference produced by a registered opt-in category SHALL resolve for triage without
that category joining the default attention union, so that any reference a due-state count
publishes can be dismissed, snoozed, or reopened by the agent it was published to.

A reference whose stored due-state entry names one page and one finding SHALL resolve, for
item review and triage, from that entry and a re-check of its category on that page alone,
without a whole-vault audit. It SHALL answer only when the re-check reproduces the stored
identity, a single category and the stored fingerprint, and SHALL otherwise resolve through
the default union first, then the opt-in categories. Such a reference addresses the due signal
alone: a decision through it SHALL record against that signal, and the review surface SHALL
keep an item open for any page-level signals folded into it. A caller that supplies the review
surface's fingerprint as the expected fingerprint SHALL resolve the item the review surface
lists, with the identity and fingerprint it has today.

Absent optional fields SHALL mean what they mean today: a page carrying no supersession
pointer and a page whose authored date is unparseable SHALL never surface in these
categories. No category SHALL alter retrieval ranking.

#### Scenario: An aging unanswered question surfaces as a candidate

- **WHEN** a governed question unit sits on an active page older than the configured age with
  no `verdict` and no answering relation authored on the unit
- **THEN** the unit appears as an open review item in the `question_aging` category at `info`
  severity, described as a review candidate rather than a defect
- **AND** an `attention` call made without a category filter does not surface it

#### Scenario: A dangling supersession pointer surfaces as a defect

- **WHEN** a page's `superseded_by` or `supersedes` pointer names a target that does not
  resolve to a page in the vault
- **THEN** a `supersession_integrity` finding is emitted for that page at `warn` severity
- **AND** an `attention` call made without a category filter surfaces it

#### Scenario: A forked chain reports more than one current head

- **WHEN** two pages both supersede the same predecessor and neither is itself superseded
- **THEN** a `supersession_integrity` finding reports the chain as carrying more than one
  current head

#### Scenario: Dismissal and material change behave like every other queue

- **WHEN** a due-state item is dismissed and the underlying page later changes materially
- **THEN** the same fingerprint never reappears
- **AND** the changed state surfaces as a new fingerprint

#### Scenario: A due reference is acted on without a whole-vault audit

- **WHEN** an agent reviews and then dismisses a reference the due-state block published, whose stored entry names one page
- **THEN** neither call runs a whole-vault audit
- **AND** the due-state count stops counting that item

#### Scenario: A round-tripped review-surface fingerprint keeps the fused item

- **WHEN** a caller resolves a reference with the fingerprint the review surface showed for a fused item
- **THEN** it receives that fused item with its categories and fingerprint unchanged
