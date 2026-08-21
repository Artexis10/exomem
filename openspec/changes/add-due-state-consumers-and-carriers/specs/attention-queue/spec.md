## ADDED Requirements

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

### Requirement: A maintained due-state projection with honest invalidation

The system SHALL maintain a due-state projection holding, per category, the count of open items
and a bounded list of top item references. The projection SHALL be updated incrementally on write
for the categories a write can affect, SHALL re-bucket at day boundaries so a `check_by` passing
at midnight surfaces without any write, SHALL be healed by the reconcile path after out-of-band
edits, and SHALL fall back to full recomputation when its persisted state is missing or
unreadable. The projection SHALL never be computed by running the full audit synchronously
inside a mutation.

Day-boundary re-bucketing SHALL be a comparison against stored dates rather than a re-scan: the
projection SHALL persist, per category and per page, both the open items and the pending
candidates that are not yet due together with the date each becomes due.

The projection SHALL be computed per audience after egress projection: an item the requesting
audience may not see SHALL contribute nothing to any count, reference list, or ordering, on any
surface, and the absence SHALL be indistinguishable from the item not existing.

#### Scenario: Midnight surfaces a prediction without a write

- **WHEN** a prediction's `check_by` date passes with no intervening mutation
- **THEN** the next served projection reflects the new open item

#### Scenario: A hidden item counts zero everywhere

- **WHEN** a due item exists on a page the requesting audience may not see
- **THEN** every count, reference list, and ordering served to that audience is identical to a
  vault where the item does not exist

#### Scenario: Damaged projection state recovers by recomputation

- **WHEN** the persisted projection is missing or unreadable at serve time
- **THEN** the projection is recomputed from canonical state
- **AND** no mutation fails or is delayed beyond the write-latency gates
