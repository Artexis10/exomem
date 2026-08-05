# attention-queue Specification

## Purpose
Give reviewers one daily, measurement-only front door over the default
`bridge_review`, `corpus_contradictions`, `stale_review`,
`unprocessed_source`, and `relation_debt` queues while retaining opt-in access
to registered typed semantic categories. The operation deterministically ranks
and deduplicates existing audit findings via Reciprocal Rank Fusion, never mutates
the vault or changes `find` ranking, reports explicit truncation, and keeps
bridge-review causes private and read-only.

## Requirements
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
- **AND** no file under the vault is created, modified, moved, or deleted

#### Scenario: Category subset and registered-category validation

- **WHEN** `attention` is called with `categories=["bridge_review"]`
- **THEN** only bridge-review items are surfaced
- **AND** `bridge_review`, `relation_debt`, and registered typed semantic
  categories are accepted, while an unregistered category raises a `ValueError`
  naming the valid set

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

#### Scenario: Rank-major interleave at equal weights

- **WHEN** each default queue contributes several findings in its emission order
- **THEN** equal weights surface every queue's rank-1 before any rank-2, with ties
  broken by the effective category preference
- **AND** running the ranking twice over identical findings yields identical output

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

### Requirement: Multi-Signal Additivity With Dedup By Anchor

The system SHALL dedup items by anchor path into one item per path carrying a `reasons`
list (one reason per contributing finding), and a path flagged by more than one queue
SHALL receive the sum of its per-queue RRF votes so it ranks above any item flagged by
only one queue at the same per-queue rank. A `corpus_contradictions` pair SHALL surface
under its anchor path with the other endpoint preserved in the reason's `related_paths`;
the second endpoint SHALL NOT become its own item unless independently flagged.

#### Scenario: A doubly-flagged note rises and keeps both reasons

- **WHEN** note `N` appears in both `stale_review` and as a `corpus_contradictions`
  anchor
- **THEN** `N` is a single item whose `categories` lists both, whose `reasons` holds both
  findings, and whose score equals the sum of the two RRF votes
- **AND** `N` ranks above an otherwise-equivalent item flagged by only one queue

#### Scenario: Contradiction pair preserved under its anchor

- **WHEN** a contradiction finding has `path=A` and `paths=[A,B]`
- **THEN** the item's path is `A` and its contradiction reason carries
  `related_paths=[A,B]`
- **AND** `B` is not surfaced as its own item unless `B` is independently flagged

### Requirement: Capped Surfacing With Explicit Counts

The system SHALL cap the surfaced items at `limit` and SHALL report the number of items
not shown (`truncated`) plus the number of contradiction pairs the upstream
`corpus_contradictions` cap (`EXOMEM_CONTRADICTION_TOP_N`) itself omitted
(`upstream_truncated`), folding the contradiction queue's trailing summary finding into
that count rather than surfacing it as a review item. It MUST NOT silently truncate:
whenever either count is non-zero it SHALL include an explanatory `note`. A `limit` of
`0` or negative SHALL disable the cap and surface all items (mirroring
`EXOMEM_CONTRADICTION_TOP_N`'s `0 = uncapped`).

#### Scenario: Items beyond the limit are counted

- **WHEN** more eligible items exist than `limit`
- **THEN** exactly `limit` items are surfaced, `truncated` equals the remainder, and a
  `note` states how many more are not shown
- **AND** when `limit` exceeds the eligible count, `truncated` is 0 and no `note` is added

#### Scenario: Non-positive limit surfaces everything

- **WHEN** `limit` is `0` or negative
- **THEN** every eligible item is surfaced, `truncated` is 0, and no `note` is added for
  the cap

#### Scenario: Upstream contradiction cap is folded, not shown

- **WHEN** the `corpus_contradictions` queue emits its trailing summary finding
  reporting upstream-capped pairs
- **THEN** that finding is not surfaced as a review item, `upstream_truncated` carries
  its count, and the `note` reports the upstream-capped pairs separately

### Requirement: Measurement-Only Composition

The composition SHALL be measurement-only. The system MUST NOT mutate any note, MUST NOT
auto-supersede, and MUST NOT change `find` ranking. It MUST NOT read, embed, or compare
note content at attention time beyond the deterministic rank arithmetic over findings
the checks already produced, and MUST NOT perform any cross-item synthesis or judgment.
Each surfaced item SHALL carry review-only guidance that defers the keep / supersede /
reconcile / compile / archive decision to the reader.

#### Scenario: Attention run leaves the vault and find untouched

- **WHEN** `attention` runs over a vault
- **THEN** no file under the vault is created, modified, moved, or deleted
- **AND** `find` ranking is unchanged
- **AND** each item's guidance states the ranking is a measurement, not a judgment that
  anything is wrong, and that nothing is auto-acted

### Requirement: Single Front-Door Command On All Surfaces

The `attention` operation SHALL be defined by a single command-registry entry and SHALL
be reachable as an MCP tool, a REST route (`/api/attention`), and a CLI subcommand
(`kb attention`) with no per-surface code, with its parameters derived as exactly
`categories` and `limit`. Its MCP description SHALL position it as the daily review
front door and SHALL defer the full lint/health report to `audit` so natural-language
tool selection routes correctly.

#### Scenario: One registry entry exposes attention everywhere

- **WHEN** the registry is built
- **THEN** an `attention` MCP tool, an `/api/attention` REST route, and a `kb attention`
  CLI subcommand all exist from the one entry
- **AND** the tool's derived parameters are exactly `categories` and `limit`
