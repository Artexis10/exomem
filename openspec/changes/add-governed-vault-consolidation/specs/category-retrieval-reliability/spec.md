## MODIFIED Requirements

### Requirement: FTS-Independent Semantic Catalog

The system SHALL maintain exact page and semantic-unit category/kind metadata in normal SQLite tables whose schema, readiness, and queries do not depend on FTS5 or trigram availability. The catalog SHALL be complete only when its stored freshness checkpoint matches a live snapshot or atomically applied complete delta AND its semantic projection identity matches the current catalog schema version, semantic-unit parser version, and core category/authoring-contract identity. Catalog rows SHALL hold neutral structural occurrences with their raw labels, and each operation SHALL interpret them through its selected registry definitions, so an extension registry change needs no rebuild. Any identity mismatch SHALL be rebuildable stale state. A missing or unverifiably stale catalog MUST NOT be interpreted as an authoritative empty index.

#### Scenario: Lean SQLite still has exact metadata

- **WHEN** FTS5 probing fails but the semantic catalog is current
- **THEN** exact category/kind parent and unit candidates are returned from normal indexed tables
- **AND** content-ranking lanes may degrade independently without changing metadata completeness

#### Scenario: Category semantics change without a note edit

- **WHEN** a sidecar built before the portable core contains authored `[constraints]` and the core contract later resolves it to `constraint` without changing that note
- **THEN** semantic projection identity mismatch prevents the old catalog from being treated as complete
- **AND** after rebuild, exact `constraint` retrieval returns the parent

#### Scenario: Extension registry save invalidates category candidates

- **WHEN** the extension semantic-language registry content hash changes without editing affected notes
- **THEN** the next exact category query interprets the unchanged structural rows through the current definitions without a rebuild
- **AND** exact category recall never returns a unit under a category that the current definitions no longer assign
