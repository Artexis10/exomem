# live-index-freshness (delta)

## ADDED Requirements

### Requirement: Typed-Graph Sidecar Freshness Token
When the `find` graph lane can read the typed epistemic-graph sidecar, the
`find` hot-cache freshness key SHALL include the sidecar's content generation
token so cached rankings are invalidated if and only if graph content, schema
version, or registry/extension state changes. The token SHALL be an in-band
generation value maintained on sidecar writes (mirroring the embedding and
lexical tokens' rationale), NOT the sidecar file mtime — WAL-checkpoint timing
moves mtime independent of content and leaves uncheckpointed commits unmoved.
When the sidecar is unavailable the key SHALL include a stable
absent-sentinel so fallback-mode entries never collide with typed-mode
entries.

#### Scenario: Graph edit invalidates cached ranking
- **WHEN** a cached `find` result exists and a Markdown write adds a typed
  relation that the graph dual-write indexes into the sidecar
- **THEN** the next identical `find` call misses the hot cache and re-ranks
  with the new edge

#### Scenario: WAL checkpoint does not evict
- **WHEN** the sidecar file's mtime changes due to a WAL checkpoint with no
  content change
- **THEN** previously cached `find` entries still hit

#### Scenario: Sidecar availability flip separates cache entries
- **WHEN** the sidecar becomes unavailable (e.g. registry drift) between two
  identical `find` calls
- **THEN** the second call does not reuse the typed-mode cached entry
