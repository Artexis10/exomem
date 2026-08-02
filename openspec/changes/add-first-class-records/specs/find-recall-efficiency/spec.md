## ADDED Requirements

### Requirement: Collection summaries are discoverable without raw-record flooding
Ordinary recall SHALL index and return collection manifests or explicitly marked bounded collection summaries. One centralized corpus/path predicate SHALL classify raw canonical material under `Knowledge Base/Records/**`—including Markdown item files and chronological logs—as structured-only by location, while allowing `_collection.md` and explicit summaries into ordinary recall. Structured-only material SHALL remain addressable by collection-scoped reference and queryable through `record_memory`. Raw CSV/TSV/JSON rows SHALL remain structured-query data rather than semantic hits.

#### Scenario: Thousand record files do not flood ordinary recall
- **WHEN** a collection contains one thousand Markdown item files under its Records canonical path
- **THEN** ordinary `ask_memory` returns no repetitive raw item hits while the collection manifest remains discoverable

#### Scenario: Explicit structured query still reaches suppressed items
- **WHEN** a caller queries that collection through `record_memory`
- **THEN** authorized matching items are returned within structured-query caps despite their exclusion from ordinary semantic recall

#### Scenario: Stable reference still resolves
- **WHEN** a caller supplies the stable identity of a structured-only Markdown item
- **THEN** direct collection-aware resolution can find it subject to governance even though semantic candidate indexes omit it

### Requirement: Suppression is consistent and reconcilable
The structured-only predicate SHALL have one owner in the shared corpus/path layer and SHALL be reused consistently by current and incremental BM25/FTS, embedding, graph, filter-only, auto-widen, move/delete, and find-candidate paths. Manual edits, moves, deletions, or policy changes SHALL remove stale derived rows and preserve collection-level discoverability. `maintain_memory(mode="reconcile", dry_run=false)` SHALL own derived-index repair; `record_memory(action="inspect")` SHALL remain report-only.

#### Scenario: Reconcile removes stale indexed item
- **WHEN** a formerly indexed record page becomes structured-only
- **THEN** reconciliation removes its lexical, vector, and graph candidate rows without deleting the canonical Markdown file

#### Scenario: Single chronological log yields one page candidate
- **WHEN** an X3-style log contains hundreds of sessions
- **THEN** ordinary recall returns the collection manifest or an explicit bounded summary, never raw session or movement items from the canonical log

#### Scenario: Legacy tracker remains explicitly inspectable
- **WHEN** a manifest-less legacy tracker falls under the structured-only Records path
- **THEN** explicit Records discovery/inspection can still identify the tracker subject to governance even though ordinary semantic recall does not index its raw body

### Requirement: Record query scale is explicitly bounded
Structured Records query SHALL apply file-size, parsed-item, returned-row, aggregate-cardinality, and response-size bounds with pagination/snapshot metadata. Optional derived caching SHALL be soft-fail and rebuildable; no heavy resident database or model SHALL be required for correctness.

#### Scenario: Oversized collection refuses or pages safely
- **WHEN** a collection exceeds a documented parse or response bound
- **THEN** the query refuses with actionable split/index guidance or returns a bounded page, never an unbounded dump or partial answer presented as complete

#### Scenario: Optional cache failure preserves correctness
- **WHEN** a derived collection cache is absent, stale, corrupt, or disabled
- **THEN** the bounded canonical-file path remains correct or the operation refuses explicitly without promoting cache state to truth
