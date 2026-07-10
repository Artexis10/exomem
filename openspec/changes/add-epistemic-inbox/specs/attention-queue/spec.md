## MODIFIED Requirements

### Requirement: Unified Review Surface Composed From The Epistemic Queues

The system SHALL provide a single `attention` operation that composes the
`stale_review`, `corpus_contradictions`, `unprocessed_source`, and `relation_debt`
review queues into one ranked list of review items, computed from a single `audit`
pass over those categories. It SHALL accept an optional `categories` subset (each
value one of the four queue names) and an optional `limit` (default 25), and SHALL
reject any category outside the four with a clear error. It MUST NOT re-implement
the queues — it consumes the findings the existing checks already produce.

#### Scenario: All four queues compose into one list

- **WHEN** `attention` is called with no `categories` filter over a vault that has
  stale, contradiction, unprocessed-source, and relation-debt findings
- **THEN** it returns a single `items` list drawn from all four queues, plus a
  `summary` of the contributing-finding count per category
- **AND** no governed note under the vault is created, modified, moved, or deleted

#### Scenario: Category subset and invalid category

- **WHEN** `attention` is called with `categories=["relation_debt"]`
- **THEN** only relation-debt items are surfaced
- **AND** calling it with a category outside {corpus_contradictions, stale_review,
  unprocessed_source, relation_debt} raises a `ValueError` naming the valid set

### Requirement: Deterministic Cross-Queue Ranking By Reciprocal Rank Fusion

The system SHALL rank the composed items by Reciprocal Rank Fusion over each finding's
intra-queue position (the queues already emit findings in rank order), reusing the
shared `reciprocal_rank_fusion_weighted` utility with `k=60` and equal default
per-category weights. The ranking SHALL be fully deterministic: identical input
findings SHALL produce a byte-identical ordering, with ties broken by a fixed category
preference (`corpus_contradictions` > `stale_review` > `unprocessed_source` >
`relation_debt`) then path.

#### Scenario: Rank-major interleave at equal weights

- **WHEN** each queue contributes several findings in its emission order
- **THEN** with equal weights the surfaced order interleaves rank-major, category-minor
  (each queue's rank-1 before any rank-2), broken by the fixed category preference
- **AND** running the ranking twice over the same findings yields identical output

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
