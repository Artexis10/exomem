# attention-queue Specification

## Purpose
Give reviewers a single daily front door instead of three separate audit
queues: a unified `attention` operation that composes `stale_review`,
`corpus_contradictions`, and `unprocessed_source` findings into one
deterministically ranked, deduplicated list via Reciprocal Rank Fusion.
Composition is measurement-only — it never mutates the vault, auto-supersedes,
or changes `find` ranking — and any capped output reports an explicit count
rather than silently truncating.
## Requirements
### Requirement: Unified Review Surface Composed From The Epistemic Queues

The system SHALL provide a single `attention` operation that composes the
`stale_review`, `corpus_contradictions`, and `unprocessed_source` review queues into
one ranked list of review items, computed from a single `audit` pass over those
categories. It SHALL accept an optional `categories` subset (each value one of the
three queue names) and an optional `limit` (default 25), and SHALL reject any category
outside the three with a clear error. It MUST NOT re-implement the queues — it consumes
the findings the existing checks already produce.

#### Scenario: All three queues compose into one list

- **WHEN** `attention` is called with no `categories` filter over a vault that has stale,
  contradiction, and unprocessed findings
- **THEN** it returns a single `items` list drawn from all three queues, plus a
  `summary` of the contributing-finding count per category
- **AND** no file under the vault is created, modified, moved, or deleted

#### Scenario: Category subset and invalid category

- **WHEN** `attention` is called with `categories=["stale_review"]`
- **THEN** only stale-review items are surfaced
- **AND** calling it with a category outside {corpus_contradictions, stale_review,
  unprocessed_source} raises a `ValueError` naming the valid set

### Requirement: Deterministic Cross-Queue Ranking By Reciprocal Rank Fusion

The system SHALL rank the composed items by Reciprocal Rank Fusion over each finding's
intra-queue position (the queues already emit findings in rank order), reusing the
shared `reciprocal_rank_fusion_weighted` utility with `k=60` and equal default
per-category weights. The ranking SHALL be fully deterministic: identical input
findings SHALL produce a byte-identical ordering, with ties broken by a fixed category
preference (`corpus_contradictions` > `stale_review` > `unprocessed_source`) then path.

#### Scenario: Rank-major interleave at equal weights

- **WHEN** each queue contributes several findings in its emission order
- **THEN** with equal weights the surfaced order interleaves rank-major, category-minor
  (each queue's rank-1 before any rank-2), broken by the fixed category preference
- **AND** running the ranking twice over the same findings yields identical output

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

