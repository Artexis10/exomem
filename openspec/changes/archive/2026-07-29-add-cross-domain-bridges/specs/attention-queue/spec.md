# attention-queue

## MODIFIED Requirements

### Requirement: Unified Review Surface Composed From The Epistemic Queues

The default `attention` category union SHALL preserve the existing review queues
and the already-shipped `relation_debt` queue while adding `bridge_review`. Its
default category and tiebreak-preference order SHALL be `bridge_review`,
`corpus_contradictions`, `stale_review`, `unprocessed_source`, and
`relation_debt`. The broader registered attention category set SHALL continue to
admit its existing typed semantic categories. `attention` SHALL consume one audit
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
- **THEN** it composes bridge review, contradiction, stale-review, unprocessed
  source, and relation-debt findings in the stated default order without removing
  any existing queue

#### Scenario: Source drift produces a private bridge finding

- **WHEN** an approved dependency changes, is deleted, or resolves ambiguously
- **THEN** the bridge surfaces with a generic `bridge_review` cause and no source
  provenance

### Requirement: Deterministic Cross-Queue Ranking By Reciprocal Rank Fusion

The system SHALL rank the effective category union by the existing deterministic
weighted Reciprocal Rank Fusion over each queue's emission order, with `k=60` and
equal default weights. It SHALL deduplicate by anchor path (partitioned by a
bridge review's audience partition where present). Identical inputs SHALL order
by score descending, then the best contributing category in this exact preference
order: `bridge_review`, `corpus_contradictions`, `stale_review`,
`unprocessed_source`, `relation_debt`, then path and partition. Reasons and
category lists SHALL use that same category preference after their intra-queue
rank.

`meta.signal_version` for `bridge_review` SHALL deterministically include bridge
bytes, review date, approval identity, cause, and dependency-state digest, and
SHALL exclude today. A due date SHALL surface review work without expiring an
otherwise exact release. Triage, dismissal, and snooze SHALL neither approve nor
renew a bridge; a dismissed item SHALL resurface only when facts change. Audience
partitions SHALL be independent. Exact reapproval of current bridge and dependency
state SHALL clear stale findings.

#### Scenario: Equal-score categories use the effective tiebreak

- **WHEN** equal-weight queues contribute equal-score items
- **THEN** the deterministic order prefers bridge review, contradiction, stale
  review, unprocessed source, then relation debt before path/partition

#### Scenario: Due review remains stable across dates

- **WHEN** the same due bridge is scanned on different later dates without a fact
  change
- **THEN** its signal version remains stable and its release is not expired solely
  by the due date

#### Scenario: Exact reapproval clears a stale review

- **WHEN** a stale bridge receives a new exact release approval after review
- **THEN** the stale bridge-review finding no longer appears for that audience
