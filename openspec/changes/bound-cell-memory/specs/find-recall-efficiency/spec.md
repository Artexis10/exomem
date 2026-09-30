## MODIFIED Requirements

### Requirement: Bounded Parsed-Page Cache

The parsed-page (frontmatter) cache SHALL be bounded both by entry count and by the total size of the parsed pages it holds, with least-recently-used eviction and an environment-variable override for each bound. Existing mtime-based invalidation semantics MUST be preserved for entries within the bounds.

#### Scenario: Cache respects its bound

- **WHEN** more distinct pages than the configured bound are parsed in one process
- **THEN** the cache size never exceeds the bound
- **AND** the least recently used entries are the ones evicted

#### Scenario: Cache respects its byte budget

- **WHEN** pages whose combined parsed size exceeds the configured byte budget are parsed in one process
- **THEN** the cache's total parsed size never exceeds the budget
- **AND** the least recently used entries are the ones evicted

#### Scenario: Warm behavior preserved within bound

- **WHEN** a page within the bound is re-requested without modification
- **THEN** it is served from cache exactly as before the change
- **AND** modifying the file still invalidates its entry via mtime

## ADDED Requirements

### Requirement: A Busy Lexical Sidecar Never Triggers A Whole-Corpus Python Index

When the FTS5 lexical sidecar exists but is transiently unavailable (locked, busy, or lagging behind vault writes), the system SHALL decline to build the whole-corpus Python BM25 index as a fallback. A declined search SHALL report the lexical lane as unavailable with its reason while the other lanes answer, and lexical search SHALL retry through the sidecar on a later pass. The Python BM25 index MAY be built only when the sidecar is absent or an explicit Python backend is requested. A catalogue whose schema is not current MAY be served by the in-process rung until it is rebuilt.

#### Scenario: A locked sidecar does not build the Python index

- **WHEN** a lexical warm or search finds the FTS5 sidecar present but locked or lagging behind vault writes
- **THEN** no whole-corpus Python BM25 index is built
- **AND** the search reports the lexical lane as unavailable with its reason
- **AND** a later pass serves lexical search from the sidecar once it is available

#### Scenario: A catalogue whose schema is not current is served in-process

- **WHEN** a lexical search finds the FTS5 sidecar's catalogue at a schema version that is not current
- **THEN** the in-process rung may serve the search
- **AND** the sidecar serves again once its catalogue is rebuilt at the current schema
