## ADDED Requirements

### Requirement: Relation registry changes invalidate derived graph resolution
The graph sidecar SHALL record core registry version and extension-registry
content hash. A mismatch SHALL be detectable as graph drift and SHALL cause the
next explicit rebuild/reconcile path to re-resolve every edge deterministically.
Registry invalidation MUST NOT modify Markdown.

#### Scenario: Alias change rebuilds canonical edge identity
- **WHEN** a valid extension registry alias changes and reconcile runs
- **THEN** the graph sidecar is rebuilt against the new registry hash, raw
  observations remain unchanged, and canonical relation metadata reflects the
  new valid resolution

### Requirement: Traversal profile changes invalidate context plans only
The system SHALL hash governed traversal profiles for cache freshness. A
profile-only change SHALL invalidate cached profile/context plans but MUST NOT
force graph edge reindexing because stored edge resolution is unchanged.

#### Scenario: Profile edit avoids unnecessary graph rebuild
- **WHEN** a valid custom profile changes its included relation families
- **THEN** the next context call uses the new profile and no Markdown or graph
  edge rows are rewritten solely because of that profile edit
