## ADDED Requirements

### Requirement: Graph Sidecar Incremental Freshness
The system SHALL maintain graph sidecar freshness from the same writer and watcher event streams that maintain other derived indexes. When a Markdown file changes, the graph index SHALL update or remove only graph rows contributed by the affected path when possible. The resulting graph SHALL be equivalent to a full rebuild over the same vault state.

#### Scenario: Single-file edit updates only affected graph rows
- **WHEN** one governed Markdown file changes and the graph index is notified
- **THEN** graph nodes and edges contributed by that file are refreshed
- **AND** unchanged files do not need to be re-read for graph rows unrelated to the changed file

#### Scenario: Incremental graph matches full rebuild
- **WHEN** the same sequence of Markdown changes is applied once through incremental graph updates and once through a full graph rebuild
- **THEN** graph context for any affected seed returns equivalent nodes, edges, and provenance

### Requirement: Graph Drift Is Auditable And Reconciled
The system SHALL detect graph sidecar drift caused by missing rows, stale source hashes, schema mismatch, or a missing sidecar. Audit/reconcile SHALL surface and repair graph drift without mutating canonical Markdown content. When graph indexing is disabled, graph drift checks SHALL short-circuit cleanly.

#### Scenario: Reconcile rebuilds stale graph rows
- **WHEN** graph audit detects that graph rows for a Markdown file are stale relative to the file's current content
- **THEN** reconcile refreshes the graph rows for that file or rebuilds the graph sidecar
- **AND** the source Markdown file remains unchanged

#### Scenario: Disabled graph indexing is a no-op
- **WHEN** graph indexing is disabled
- **THEN** graph drift checks return no actionable findings
- **AND** no optional graph dependency or sidecar is required

### Requirement: Graph Freshness Cannot Hide External Edits
Self-write suppression for Exomem-authored filesystem events SHALL NOT hide later external edits from graph maintenance. A later edit, delete, or move observed from Obsidian, mobile sync, manual filesystem changes, or git operations SHALL update graph freshness or be repaired by reconcile.

#### Scenario: External edit after self-write refreshes graph
- **WHEN** Exomem writes a note and suppresses its own watcher echo
- **AND** a later external edit changes that same note
- **THEN** graph freshness treats the later event as external
- **AND** the graph rows for the edited note are refreshed or marked stale for reconcile

#### Scenario: Missed graph event is healed
- **WHEN** a filesystem event that should refresh graph rows is missed
- **THEN** periodic or user-invoked reconcile detects the graph freshness mismatch
- **AND** the graph sidecar is corrected to match the on-disk Markdown state
