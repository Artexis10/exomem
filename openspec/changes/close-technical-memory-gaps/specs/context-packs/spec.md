## ADDED Requirements

### Requirement: Unified bounded memory context
`connect_memory(operation="context")` SHALL accept a query, path, or stable reference and return seed nodes, semantic blocks, typed edges, source/evidence provenance, supersession history, warnings, and explicit truncation. `graph-context` SHALL remain a compatibility alias.

#### Scenario: Context follows evidence and history
- **WHEN** context is requested for a source-backed conclusion that supersedes an earlier version and cites evidence
- **THEN** the response includes the active and prior conclusion, their supersession edge, the source/evidence nodes, and provenance paths within configured bounds

### Requirement: Context assembly remains measurement-only
Unified context SHALL use deterministic parsing, stored relations, retrieval, and precomputed model measurements only. It MUST NOT generate summaries, accept suggested relations, change retrieval ranking, or mutate the vault.

#### Scenario: Context request is read-only
- **WHEN** unified context is assembled with graph enrichment
- **THEN** no vault file changes and returned excerpts are sourced from stored content with provenance

### Requirement: Unresolved observed relations remain visible
The graph SHALL represent an observed edge to a missing target with a typed placeholder node rather than silently dropping the edge during traversal.

#### Scenario: Forward reference appears in context
- **WHEN** a semantic relation points to a not-yet-created page
- **THEN** context includes the observed edge and an unresolved placeholder carrying the original target
