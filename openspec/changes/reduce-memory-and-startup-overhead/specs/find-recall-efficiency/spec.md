## ADDED Requirements

### Requirement: Vector Metadata Residency Bound

The in-memory (numpy) vector backend SHALL NOT hold per-chunk text in its process-resident
query cache; chunk metadata beyond what scoring requires MUST be joined from the embedding
sidecar by rowid at result-materialization time. Ranking output MUST remain identical to the
prior behavior, gated by the golden retrieval floors and the existing backend parity tests.

#### Scenario: Chunk text absent from resident cache

- **WHEN** the numpy vector backend has served a query pass over a built sidecar
- **THEN** its process-resident cache holds vectors and rowid-level metadata only, not chunk
  text bodies

#### Scenario: Ranking unchanged

- **WHEN** the golden retrieval suite runs against the numpy backend after the change
- **THEN** all golden floors pass
- **AND** backend parity tests report identical result sets

### Requirement: Bounded Parsed-Page Cache

The parsed-page (frontmatter) cache SHALL be size-bounded with least-recently-used eviction
and an environment-variable override for the bound. Existing mtime-based invalidation
semantics MUST be preserved for entries within the bound.

#### Scenario: Cache respects its bound

- **WHEN** more distinct pages than the configured bound are parsed in one process
- **THEN** the cache size never exceeds the bound
- **AND** the least recently used entries are the ones evicted

#### Scenario: Warm behavior preserved within bound

- **WHEN** a page within the bound is re-requested without modification
- **THEN** it is served from cache exactly as before the change
- **AND** modifying the file still invalidates its entry via mtime
