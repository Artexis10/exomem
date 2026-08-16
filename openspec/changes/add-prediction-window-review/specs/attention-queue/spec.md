## ADDED Requirements

### Requirement: Prediction Window Review Queue

The audit registry SHALL expose a `prediction_window` category that surfaces a rich
semantic unit whose authored check date has come due without any recorded sign that the
unit was checked. A unit SHALL be surfaced when all of the following hold: it carries an
authored `check_by` that parses to a calendar date, that date is on or before today, it
carries no `verdict` metadata, and it carries no outbound relation whose registry-resolved
canonical kind is one of `supports`, `contradicts`, `resolves`, or `evidenced_by`.

The resolution test SHALL be unit-local. Only relations authored on the unit itself SHALL
clear it; a relation authored elsewhere on the parent page MUST NOT, and an inbound
relation authored on another page MUST NOT. Relation kinds SHALL be compared after
registry resolution to their canonical key, so a registered alias resolves correctly.

The queue SHALL exclude pages whose `status` is `superseded`, `archived`, or `draft`,
index and log pages, and pages outside a read-write access tier. Findings SHALL carry
severity `info`, SHALL anchor on the parent page's vault-relative path, and SHALL be
ordered most-overdue-first by days past `check_by`, with the parent path and the unit
fingerprint as deterministic tiebreaks.

The check SHALL be a pure measurement over authored Markdown. It MUST NOT write, judge
whether the prediction held, assign a verdict, or change `find` ordering. Its proposed fix
SHALL defer the decision to the reader.

#### Scenario: A due prediction with no verdict and no resolving relation surfaces

- **WHEN** a unit carries `check_by` of a date 14 days ago, no `verdict`, and no resolving
  relation
- **THEN** a `prediction_window` finding is emitted at `info` severity anchored on the
  parent page whose meta reports 14 overdue days
- **AND** no file under the vault is created, modified, moved, or deleted

#### Scenario: A verdict clears the window

- **WHEN** the same unit carries a `verdict`
- **THEN** no `prediction_window` finding is emitted for it

#### Scenario: An outbound resolving relation clears the window

- **WHEN** the same unit carries an outbound relation whose canonical kind is `resolves`,
  `supports`, `contradicts`, or `evidenced_by`
- **THEN** no `prediction_window` finding is emitted for it

#### Scenario: A non-resolving outbound relation does not clear the window

- **WHEN** the same unit's only outbound relation has canonical kind `relates_to`
- **THEN** a `prediction_window` finding is still emitted for it

#### Scenario: A relation on a sibling unit does not clear the window

- **WHEN** a due unit carries no relation and a different unit on the same page carries a
  resolving relation
- **THEN** a `prediction_window` finding is still emitted for the due unit

#### Scenario: A future check date is not yet due

- **WHEN** a unit carries a `check_by` date later than today
- **THEN** no `prediction_window` finding is emitted for it

#### Scenario: A unit without a check date is never surfaced

- **WHEN** a unit carries no `check_by`
- **THEN** no `prediction_window` finding is emitted for it

#### Scenario: The queue is ordered most-overdue-first

- **WHEN** several due units have different overdue ages
- **THEN** the findings are emitted with the largest overdue age first and ties broken by
  parent path and unit fingerprint

### Requirement: Prediction Window Findings Are Identified By Unit Fingerprint

Each `prediction_window` finding SHALL carry the surfaced unit's fingerprint as its
`meta.signal_version` and as its `meta.review_partition`, so that the composed review item
is per-unit rather than per-page and its durable review-state identity is bound to the
unit's authored state.

Two due units on one page SHALL therefore compose as two independent review items sharing
that page's path, each with its own review-state identity, so a decision recorded against
one MUST NOT dispose of the other. Editing a surfaced unit SHALL change its fingerprint and
therefore its review item's fingerprint, so the item resurfaces for review rather than
inheriting a decision recorded against different authored content.

#### Scenario: Two due predictions on one page are two review items

- **WHEN** a page carries two due, unresolved units and `attention` is called with the
  `prediction_window` category
- **THEN** two review items are surfaced sharing that page's path with distinct review
  identities and distinct fingerprints

#### Scenario: Editing a prediction moves its fingerprint

- **WHEN** a surfaced unit's authored content is changed and the queue is recomputed
- **THEN** the finding's `signal_version` differs from the value recorded before the edit

### Requirement: Prediction Window Is Registered But Not Default In Attention

`prediction_window` SHALL be a valid `attention` category, selectable through the existing
`categories` filter, and SHALL be rejected by neither the attention validator nor the audit
validator. It MUST NOT be added to the default attention category union, so an `attention`
call made without a category filter SHALL compose exactly the `bridge_review`,
`corpus_contradictions`, `stale_review`, `unprocessed_source`, and `relation_debt` queues
as before, in their existing tiebreak-preference order.

Selecting a category set that omits `prediction_window` SHALL reproduce the prior behaviour
of both `audit` and `attention` exactly, so the category can be disabled without residue.

#### Scenario: The default attention union is unchanged

- **WHEN** `attention` is called without a category filter over a vault holding a due
  prediction
- **THEN** no `prediction_window` item is surfaced and the composed categories are exactly
  the pre-existing default union

#### Scenario: The category is selectable

- **WHEN** `attention` is called with `categories=["prediction_window"]`
- **THEN** the due prediction is surfaced as a ranked review item carrying its reason, and
  no `ValueError` is raised for the category name

#### Scenario: The category reaches the audit surface

- **WHEN** `audit` is called with `categories=["prediction_window"]`
- **THEN** the due prediction is reported and the summary counts it under that category
  name
