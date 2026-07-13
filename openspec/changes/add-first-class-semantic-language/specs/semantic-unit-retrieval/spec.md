## ADDED Requirements

### Requirement: Page, Unit, Mixed, And Auto Recall Levels
`find` and `ask_memory` SHALL accept `result_level` values `auto`, `page`, `unit`, and `mixed`. `auto` SHALL be the default, SHALL resolve to `page` when no semantic-unit filter is present, and SHALL resolve to `unit` when `categories` or `kinds` is supplied. Existing calls with no new parameters MUST preserve page-hit fields and ordering.

#### Scenario: Existing recall remains page-level
- **WHEN** recall is called without `result_level`, `categories`, or `kinds`
- **THEN** `auto` returns the existing page-level result contract with unchanged ordering

#### Scenario: Category filter selects unit results automatically
- **WHEN** recall is called with `categories=["config"]` and `result_level` is omitted
- **THEN** the response contains independently ranked matching semantic-unit hits

#### Scenario: Page mode annotates matching units
- **WHEN** recall is called with `result_level="page"` and a unit filter
- **THEN** matching parent pages are returned with a bounded `matched_units` annotation

#### Scenario: Mixed mode bounds repetition by parent
- **WHEN** mixed recall matches a page and many of its units
- **THEN** page and unit candidates participate in ranking, repeated units per parent are capped, and any truncation is reported

### Requirement: Exact Category And Kind Filtering
Recall SHALL accept OR-within-list `categories` and `kinds` filters and SHALL apply exact registry-resolved category/governed-kind metadata filtering before ranking. Text, category, and kind axes SHALL combine with AND when supplied together. The resolved category defaults to the immutable authored category key when no alias applies. Category-only and kind-only recall SHALL work with an empty text query. Mentioning a category word in content MUST NOT satisfy an exact category filter.

#### Scenario: Text mention cannot spoof category
- **WHEN** a `decision` unit mentions the word `requirement` and recall filters `categories=["requirement"]`
- **THEN** that decision unit is excluded unless its canonical category is actually `requirement`

#### Scenario: Multiple category filter is a union
- **WHEN** recall filters `categories=["config", "rule"]`
- **THEN** units in either canonical category are eligible and other categories are excluded

#### Scenario: Text category and kind axes intersect
- **WHEN** recall supplies text `SQLite`, `categories=["config", "rule"]`, and `kinds=["decision"]`
- **THEN** a unit is eligible only if it matches the text, has either resolved category, and has governed kind `decision`

#### Scenario: Same content in different categories remains distinct
- **WHEN** two units have identical content but different categories
- **THEN** unit-level recall returns distinct identities and correct category metadata for each

### Requirement: First-Class Semantic Unit Hit Contract
Unit hits SHALL carry `result_type="semantic_unit"`, unit reference, form, `category_raw`, authored `category_key`, registry-resolved `category`, kind, content/excerpt, tags/context, source anchor/span/hash, parent path/reference/title/type/status, and applicable ranking/degradation signals. A unit citation SHALL identify both its parent page and anchor.

#### Scenario: Unit hit is independently citable
- **WHEN** a unit is returned from recall
- **THEN** the caller can cite and subsequently read that exact unit through its parent reference and unit anchor/reference

#### Scenario: Superseded parent state is visible
- **WHEN** a matching unit belongs to a superseded page
- **THEN** the unit hit reports the parent status and successor pointer and obeys the existing active-preference policy

### Requirement: Unit Lexical And Optional Vector Retrieval
The system SHALL index semantic units as parent-owned records in the existing rebuildable lexical sidecar and, when embeddings are available, in the existing embedding sidecar. Lexical/category recall SHALL remain available when embeddings are disabled, unavailable, or warming. Optional unit embeddings are deterministic measurement and MUST NOT invoke a reasoning model.

#### Scenario: Embeddings-off category recall works
- **WHEN** embeddings are disabled and recall filters a category
- **THEN** matching units are returned from deterministic metadata/lexical indexes
- **AND** no embedding model is loaded

#### Scenario: Vector failure soft-falls back
- **WHEN** unit-vector retrieval fails after startup
- **THEN** the response marks vector degradation and still returns lexical/category matches

### Requirement: Semantic Units Participate In The Epistemic Graph
The graph sidecar SHALL index compact and rich semantic units as nodes with `derived_from` edges to their parent file. A normalized rich unit SHALL preserve its existing semantic-block node key and SHALL produce exactly one graph node/edge identity. Authored rich-unit relations SHALL retain raw/canonical relation identity, origin, anchor/hash, registry status, and traversal behavior. Compact observations SHALL NOT acquire inferred typed relations.

#### Scenario: Compact unit appears as a graph node
- **WHEN** the graph is rebuilt for a page containing a compact observation
- **THEN** graph context can return the observation node, its category/kind metadata, and its `derived_from` parent edge

#### Scenario: Compact wikilink does not infer semantics
- **WHEN** a compact observation contains an ordinary wikilink but no authored typed unit relation
- **THEN** the system does not invent a typed epistemic relation for that unit

#### Scenario: Rich block is not doubled in graph results
- **WHEN** one rich semantic block is available through both the new semantic-unit surface and legacy semantic-block compatibility output
- **THEN** graph indexing/traversal returns one preserved block identity rather than parallel legacy and unit nodes

### Requirement: Parent-Owned Unit Replacement And Cleanup
Index updates SHALL replace all derived unit records owned by the affected parent in one transaction per sidecar. Every record SHALL carry one shared parent generation/source hash/parser version, and query-time consumers MUST reject records that do not match the current on-disk parent or joined generation. Edits MUST remove stale unit/category/text rows, moves MUST preserve parent-aware identity only when backed by stable parent identity, and deletes/trash MUST remove all unit lexical, vector, and graph records.

#### Scenario: Category edit removes old filter hit
- **WHEN** a unit category changes from `config` to `rule`
- **THEN** post-update recall no longer returns it for `config` and returns it for `rule`

#### Scenario: Delete removes every derived unit row
- **WHEN** a parent page is deleted or trashed through a writer or watcher event
- **THEN** none of its units remain in lexical, vector, graph, category-only, or context results

#### Scenario: Sidecar generations cannot be mixed
- **WHEN** one sidecar update fails after another commits the new parent generation
- **THEN** recall excludes old-generation records, reports degradation/index drift, and never combines stale and current unit state

### Requirement: Exact Unit Read
`read_memory` SHALL accept a unit reference and return the exact current semantic unit plus bounded parent context. A missing, stale, ambiguous, or superseded unit reference SHALL return a structured status rather than silently selecting another unit.

#### Scenario: Read one unit
- **WHEN** `read_memory` receives a current anchored unit reference
- **THEN** it returns that unit, its parent metadata, and bounded surrounding Markdown

#### Scenario: Stale anonymous reference is explicit
- **WHEN** an anonymous unit changed after a reference was issued
- **THEN** reading the old reference reports a stale reference and does not return a nearest text match
