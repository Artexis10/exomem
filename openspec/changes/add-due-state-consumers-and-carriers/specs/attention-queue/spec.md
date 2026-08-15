## ADDED Requirements

### Requirement: Due-state consumers surface the loop's time-bound obligations

The audit registry SHALL add four categories, each producing review items with the standard reference, fingerprint, dismiss/snooze/reopen, and material-change-resurfacing semantics:

- `prediction_window`: a prediction unit is due when its `check_by` date has passed, the unit carries no `verdict` metadata, and the unit itself authors no resolving relation (`supports`, `contradicts`, `resolves`, `evidenced_by`). The predicate SHALL be unit-local and SHALL NOT depend on inbound edges.
- `unfinished_experiments`: an experiment page whose elapsed time since `started` exceeds its declared `duration` and which carries no `outcome` SHALL surface, age-ordered.
- `question_aging`: an open-question unit on an active page SHALL surface after a configured age with no answering structure, reported as a review candidate, never a defect.
- `supersession_integrity`: a supersession pointer whose target does not resolve, and a chain with more than one current head, SHALL surface as defects.

Absent optional fields SHALL mean what they mean today: a prediction without `check_by` and an experiment without `duration` SHALL never surface in these categories. No category SHALL alter retrieval ranking.

#### Scenario: An overdue prediction surfaces and resolves

- **WHEN** a prediction unit's `check_by` date passes with no verdict and no resolving relation authored on the unit
- **THEN** the prediction appears as an open review item in its category
- **AND** recording a `verdict` on the unit removes it from the open view without any dismissal record

#### Scenario: A stopped experiment is asked for its outcome

- **WHEN** an experiment page's elapsed time exceeds its declared duration and no `outcome` is recorded
- **THEN** the page surfaces in the unfinished-experiments category ordered by overshoot age
- **AND** recording an `outcome` removes it from the open view

#### Scenario: Dismissal and material change behave like every other queue

- **WHEN** a due-state item is dismissed and the underlying page later changes materially
- **THEN** the same fingerprint never reappears
- **AND** the changed state surfaces as a new fingerprint

### Requirement: A maintained due-state projection with honest invalidation

The system SHALL maintain a due-state projection holding, per category, the count of open items and a bounded list of top item references. The projection SHALL be updated incrementally on write for the categories a write can affect, SHALL re-bucket at day boundaries so a `check_by` passing at midnight surfaces without any write, SHALL be healed by the reconcile path after out-of-band edits, and SHALL fall back to full recomputation when its persisted state is missing or unreadable. The projection SHALL never be computed by running the full audit synchronously inside a mutation.

The projection SHALL be computed per audience after egress projection: an item the requesting audience may not see SHALL contribute nothing to any count, reference list, or ordering, on any surface, and the absence SHALL be indistinguishable from the item not existing.

#### Scenario: Midnight surfaces a prediction without a write

- **WHEN** a prediction's `check_by` date passes with no intervening mutation
- **THEN** the next served projection reflects the new open item

#### Scenario: A hidden item counts zero everywhere

- **WHEN** a due item exists on a page the requesting audience may not see
- **THEN** every count, reference list, and ordering served to that audience is identical to a vault where the item does not exist

#### Scenario: Damaged projection state recovers by recomputation

- **WHEN** the persisted projection is missing or unreadable at serve time
- **THEN** the projection is recomputed from canonical state
- **AND** no mutation fails or is delayed beyond the write-latency gates
