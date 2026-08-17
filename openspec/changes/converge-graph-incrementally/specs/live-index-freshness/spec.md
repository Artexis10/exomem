## ADDED Requirements

### Requirement: Event-Maintained Epistemic Graph

The system SHALL maintain the epistemic graph incrementally, on the same terms as the
inbound-link index: when a specific set of markdown files changes, the system SHALL
update only the affected files' nodes, edges, and parent references rather than
re-walking the entire knowledge base.

The resulting graph's content MUST be identical to what a full rebuild would produce
for the same vault state. When incremental maintenance is not possible — no graph
sidecar exists, the schema or registry version changed, the batch scope is full, a
lineage reset occurred, or the user invoked reconcile explicitly — the system MUST fall
back to the existing full-vault rebuild, exactly as before this capability existed.

Incremental graph work SHALL be driven from the durable per-path work queue rather than
from in-memory state, so a missed or interrupted drain is repaired by the next one
rather than lost. The existing periodic reconciliation SHALL continue to bound how stale
the graph can become from work that was queued but never drained.

#### Scenario: A single-file change patches only that file's graph entries

- **WHEN** one markdown file changes and the graph is notified of that change
- **THEN** only that file's prior nodes, edges, and parent references are removed and
  recomputed
- **AND** no other file is re-read

#### Scenario: A patched graph matches a full rebuild in content

- **WHEN** the same sequence of file changes is applied once via incremental drains and
  once via a full rebuild from the resulting vault state
- **THEN** the nodes, edges, and parent references are identical between the two

#### Scenario: Incremental maintenance not possible falls back to a full rebuild

- **WHEN** no graph sidecar exists, or the schema or registry version changed, or a
  lineage reset occurred
- **THEN** the graph is computed by a full-vault rebuild, exactly as before this
  capability existed

#### Scenario: An undrained queue is bounded by reconciliation

- **WHEN** paths are queued for graph repair and no drain call site fires
- **THEN** the periodic reconciliation drains them, bounding how stale the graph becomes
