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

### Requirement: Prediction Window Reaches The Audit Surface

`prediction_window` SHALL be a valid `audit` category, selectable through the existing
`categories` filter and rejected by neither the audit validator nor the attention
validator. Selecting a category set that omits `prediction_window` SHALL reproduce the
prior behaviour of both `audit` and `attention` exactly, so the category can be disabled
without residue.

#### Scenario: The category is selectable on audit

- **WHEN** `audit` is called with `categories=["prediction_window"]`
- **THEN** the due prediction is reported and the summary counts it under that category
  name

#### Scenario: The category is selectable on attention

- **WHEN** `attention` is called with `categories=["prediction_window"]`
- **THEN** the due prediction is surfaced as a ranked review item carrying its reason, and
  no `ValueError` is raised for the category name

## MODIFIED Requirements

### Requirement: Unified Review Surface Composed From The Epistemic Queues

The default `attention` category union SHALL preserve the existing review queues
and the already-shipped `relation_debt` queue while adding `bridge_review` and
`prediction_window`. Its default category and tiebreak-preference order SHALL be
`bridge_review`, `prediction_window`, `corpus_contradictions`, `stale_review`,
`unprocessed_source`, and `relation_debt`. A queue that fires on a date the
author wrote down SHALL outrank every queue that infers its own candidates, so
`bridge_review` and `prediction_window` SHALL precede the remaining four; the
relative order of those four is historical and carries no normative claim. The
broader registered attention category set SHALL continue to
admit its existing typed semantic categories and its opt-in epistemic-lifecycle
categories. `attention` SHALL consume one audit
pass over its selected categories and SHALL remain read-only.

`bridge_review` SHALL use read-only reference resolution; it SHALL not write
canonical governance facts or review state during scanning. A release grant's
bridge SHALL surface per approved audience for only these generic,
bridge-path-anchored causes: due review date, bridge edited/stale approval,
source or relevant restriction changed, and source unavailable/ambiguous. Detail,
metadata, related paths, review context, and public responses SHALL not disclose
restricted source title, path, ref, or other provenance.

#### Scenario: Default attention composes the effective queue union

- **WHEN** `attention` is called without a category filter
- **THEN** it composes bridge review, prediction window, contradiction,
  stale-review, unprocessed source, and relation-debt findings in the stated
  default order without removing any existing queue
- **AND** no file under the vault is created, modified, moved, or deleted

#### Scenario: A due prediction reaches the daily surface unasked

- **WHEN** `attention` is called without a category filter over a vault holding a
  due, unresolved prediction
- **THEN** that prediction is surfaced as a ranked review item without the caller
  naming the `prediction_window` category

#### Scenario: Category subset and registered-category validation

- **WHEN** `attention` is called with `categories=["bridge_review"]`
- **THEN** only bridge-review items are surfaced
- **AND** `bridge_review`, `prediction_window`, `relation_debt`, and registered
  typed semantic categories are accepted, while an unregistered category raises a
  `ValueError` naming the valid set

#### Scenario: Source drift produces a private bridge finding

- **WHEN** an approved dependency changes, is deleted, or resolves ambiguously
- **THEN** the bridge surfaces with a generic `bridge_review` cause and no source
  provenance

### Requirement: Deterministic Cross-Queue Ranking By Reciprocal Rank Fusion

The system SHALL rank the effective category union by the existing deterministic
weighted Reciprocal Rank Fusion over each queue's emission order, with `k=60` and
equal default weights. It SHALL deduplicate by anchor path (partitioned by a
bridge review's audience partition or a prediction's unit partition where
present). Identical inputs SHALL order
by score descending, then the best contributing category in this exact preference
order: `bridge_review`, `prediction_window`, `corpus_contradictions`,
`stale_review`, `unprocessed_source`, `relation_debt`, then path and partition.
Reasons and category lists SHALL use that same category preference after their
intra-queue rank.

`meta.signal_version` for `bridge_review` SHALL deterministically include bridge
bytes, review date, approval identity, cause, and dependency-state digest, and
SHALL exclude today. A due date SHALL surface review work without expiring an
otherwise exact release. Triage, dismissal, and snooze SHALL neither approve nor
renew a bridge; a dismissed item SHALL resurface only when facts change. Audience
partitions SHALL be independent. Exact reapproval of current bridge and dependency
state SHALL clear stale findings.

#### Scenario: Rank-major interleave at equal weights

- **WHEN** each default queue contributes several findings in its emission order
- **THEN** equal weights surface every queue's rank-1 before any rank-2, with ties
  broken by the effective category preference
- **AND** running the ranking twice over identical findings yields identical output

#### Scenario: Equal-score categories use the effective tiebreak

- **WHEN** equal-weight queues contribute equal-score items
- **THEN** the deterministic order prefers bridge review, prediction window,
  contradiction, stale review, unprocessed source, then relation debt before
  path/partition

#### Scenario: Due review remains stable across dates

- **WHEN** the same due bridge is scanned on different later dates without a fact
  change
- **THEN** its signal version remains stable and its release is not expired solely
  by the due date

#### Scenario: Exact reapproval clears a stale review

- **WHEN** a stale bridge receives a new exact release approval after review
- **THEN** the stale bridge-review finding no longer appears for that audience

### Requirement: Multi-Signal Additivity With Dedup By Anchor

The system SHALL dedup items by anchor path into one item per path carrying a `reasons`
list (one reason per contributing finding), and a path flagged by more than one queue
SHALL receive the sum of its per-queue RRF votes so it ranks above any item flagged by
only one queue at the same per-queue rank. A `corpus_contradictions` pair SHALL surface
under its anchor path with the other endpoint preserved in the reason's `related_paths`;
the second endpoint SHALL NOT become its own item unless independently flagged.

A partitioned queue SHALL refine that anchor rather than escape it. Where a path carries
partitioned findings, the system SHALL emit one item per partition and SHALL fold that
path's unpartitioned findings into every one of them, contributing both their reasons and
their RRF votes. A path's unpartitioned findings MUST NOT form an additional item of their
own whenever that path also carries a partitioned finding, so a note is never listed once
for its page-level signals and again for a partition. A path carrying exactly one
partitioned finding SHALL therefore compose exactly one item, and the per-queue votes of
that item SHALL still sum.

#### Scenario: A doubly-flagged note rises and keeps both reasons

- **WHEN** note `N` appears in both `stale_review` and as a `corpus_contradictions`
  anchor
- **THEN** `N` is a single item whose `categories` lists both, whose `reasons` holds both
  findings, and whose score equals the sum of the two RRF votes
- **AND** `N` ranks above an otherwise-equivalent item flagged by only one queue

#### Scenario: A partitioned queue co-flagging with a page-level queue stays one row

- **WHEN** note `N` carries one due prediction and is also flagged by `stale_review`
- **THEN** `N` composes exactly one item whose `categories` lists both, whose `reasons`
  holds both findings, and whose score is the sum of the two RRF votes
- **AND** `N` ranks above an otherwise-equivalent item flagged by only one queue

#### Scenario: Several partitions each inherit the page-level reasons

- **WHEN** note `N` carries two due predictions and is also flagged by `relation_debt`
- **THEN** `N` composes two items with distinct review identities, each carrying its own
  prediction reason and the shared `relation_debt` reason
- **AND** no additional item is composed for the `relation_debt` finding alone

#### Scenario: Contradiction pair preserved under its anchor

- **WHEN** a contradiction finding has `path=A` and `paths=[A,B]`
- **THEN** the item's path is `A` and its contradiction reason carries
  `related_paths=[A,B]`
- **AND** `B` is not surfaced as its own item unless `B` is independently flagged
