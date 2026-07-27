## ADDED Requirements

### Requirement: A full rebuild does not hold the vault mutation boundary
The system SHALL perform full epistemic graph rebuild work without holding the
vault mutation boundary, acquiring it only to publish the result.

#### Scenario: Unrelated mutation during a rebuild
- **WHEN** a full graph rebuild is in progress
- **AND** an unrelated vault mutation is requested
- **THEN** that mutation proceeds without waiting for the rebuild to finish

#### Scenario: Boundary hold is bounded by the swap
- **WHEN** a full graph rebuild completes
- **THEN** the vault mutation boundary is held only for the publish step
- **AND** the hold does not scale with vault size

### Requirement: A partially rebuilt graph is never observable
The system SHALL publish a rebuilt graph as a single atomic replacement.

#### Scenario: Read during a rebuild
- **WHEN** a graph read occurs while a full rebuild is in progress
- **THEN** it observes either the previous graph state or the fully rebuilt one
- **AND** never an empty or partially populated graph

#### Scenario: Crash during a rebuild
- **WHEN** the process terminates during a full rebuild
- **THEN** the live sidecar is left in its pre-rebuild state
- **AND** the abandoned temporary database is removed by the next reconcile

### Requirement: Concurrent rebuild requests are single-flight
The system SHALL run at most one full graph rebuild per vault at a time.

#### Scenario: Several writers arrive with an unusable sidecar
- **WHEN** multiple writes require a rebuild concurrently
- **THEN** exactly one full rebuild runs
- **AND** the remaining requests resolve from that rebuild's outcome

### Requirement: Published graphs reflect a stable vault
The system SHALL verify vault freshness immediately before publishing a rebuilt
graph, while holding the vault mutation boundary.

#### Scenario: Vault changes during the final pass
- **WHEN** vault freshness changes between the start of a rebuild pass and the publish step
- **THEN** the rebuild is retried rather than published

#### Scenario: Rebuild cannot observe a stable vault
- **WHEN** a rebuild exhausts its stabilization attempts
- **THEN** it does not publish a graph
- **AND** the graph is marked unavailable

### Requirement: The write path still yields an available graph
The system SHALL preserve the existing write-path contract when the sidecar is
unusable.

#### Scenario: Write against a missing sidecar
- **WHEN** a write occurs and the graph sidecar is missing, schema-mismatched, or registry-invalidated
- **THEN** the graph is rebuilt across the whole vault
- **AND** the graph reports itself available once the write returns
