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
broader registered attention category set SHALL continue to admit its existing
typed semantic categories and its opt-in epistemic-lifecycle categories.
`attention` SHALL consume one audit pass over its selected categories and SHALL
remain read-only. It MUST NOT re-implement the queues — it consumes the findings
the existing checks already produce.

`bridge_review` SHALL use read-only reference resolution; it SHALL not write
canonical governance facts or review state during scanning. A release grant's
bridge SHALL surface per approved audience for only these generic,
bridge-path-anchored causes: due review date, bridge edited/stale approval,
source or relevant restriction changed, and source unavailable/ambiguous. Detail,
metadata, related paths, review context, and public responses SHALL not disclose
restricted source title, path, ref, or other provenance.

#### Scenario: All four queues compose into one list

- **WHEN** `attention` is called with the historical four-queue category subset over a vault that has
  stale, contradiction, unprocessed-source, and relation-debt findings
- **THEN** it returns a single `items` list drawn from all four queues, plus a
  `summary` of the contributing-finding count per category
- **AND** no governed note under the vault is created, modified, moved, or deleted

#### Scenario: Category subset and invalid category

- **WHEN** `attention` is called with `categories=["relation_debt"]`
- **THEN** only relation-debt items are surfaced
- **AND** calling it with an unregistered category raises a `ValueError` naming
  the complete registered set

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
present). Identical inputs SHALL order by score descending, then the best
contributing category in this exact preference order: `bridge_review`,
`prediction_window`, `corpus_contradictions`, `stale_review`,
`unprocessed_source`, `relation_debt`, then path and partition. Reasons and
category lists SHALL use that same category preference after their intra-queue
rank.

`meta.signal_version` for `bridge_review` SHALL deterministically include bridge
bytes, review date, approval identity, cause, and dependency-state digest, and
SHALL exclude today. A due date SHALL surface review work without expiring an
otherwise exact release. Triage, dismissal, and snooze SHALL neither approve nor
renew a bridge; a dismissed item SHALL resurface only when facts change. Audience
partitions SHALL be independent. Exact reapproval of current bridge and
dependency state SHALL clear stale findings.

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

## ADDED Requirements

### Requirement: Relation debt is a deterministic attention source
The `relation_debt` audit SHALL surface active, writable compiled pages with no outbound body wikilinks or canonical note/block relations. It SHALL exclude append-only, read-only, archived, superseded, index, hub, and snapshot material. Each finding SHALL be informational, include a content-derived signal version, and propose relation/link review rather than automatic mutation.

#### Scenario: Isolated compiled note is surfaced
- **WHEN** an active writable research note has no outbound links or typed relations
- **THEN** relation debt emits one informational finding for that page
- **AND** adding a canonical relation removes the finding on the next audit

### Requirement: Review state filters after ranking without changing scores
Attention SHALL compute deterministic base scores before applying review state. It SHALL support `open` (default), `all`, `snoozed`, and `dismissed` state views, fill visible items up to the requested limit after filtering, and report hidden-state counts separately. State filtering SHALL NOT change the score or relative order of the remaining items.

#### Scenario: Hidden top item does not waste the visible limit
- **WHEN** the highest-ranked item is dismissed and attention is requested with `state="open"` and `limit=5`
- **THEN** the next five open items are returned in their original relative order
- **AND** the report counts the dismissed item separately
