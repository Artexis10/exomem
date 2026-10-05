# simple-command-surface Specification

## Purpose
Define a small set of simple product actions (`ask`, remember and capture, review, connect, adopt, maintain) that route common knowledge-base intents to canonical operations without duplicating command logic or weakening existing safety contracts.

## Requirements

### Requirement: Simple Product Actions

The system SHALL define a small set of simple product actions that route common knowledge-base intents to canonical Exomem operations without duplicating command logic. The action catalog SHALL be capability-complete over the product command registry, and SHALL be available to agent surfaces as well as CLI-family surfaces.

#### Scenario: Action catalog names the simple actions

- **WHEN** the action catalog is requested by bootstrap, docs generation, or CLI help
- **THEN** it lists `ask`, `remember`, `capture`, `review`, `connect`, `adopt`, `maintain`, `record`, and `plan`
- **AND** each action identifies its canonical operation route and default safety posture
- **AND** the catalog is generated from the action registry, so an action added to the registry appears without editing a second list

#### Scenario: Canonical operations remain the source of truth

- **WHEN** a simple action invokes an Exomem operation
- **THEN** it uses the existing registry leaf and validation path
- **AND** it does not bypass guarded fields, destructive-operation metadata, schema validation, or vault path checks

#### Scenario: Every product command is reachable from an action

- **WHEN** the action catalog is resolved with no surface restriction
- **THEN** every command in the product command registry is named by at least one action's primary route, alternate route, or `advanced` list, except commands that are themselves the catalog entry point
- **AND** a product command that no action reaches fails the surface-contract check rather than being silently unreachable

#### Scenario: Action catalog is delivered to an agent surface

- **WHEN** an agent surface requests the action catalog through its bootstrap contract
- **THEN** the catalog resolves against exactly the commands that surface exports
- **AND** an action with no surviving route is reported unavailable with its reason rather than omitted
- **AND** the action names are not themselves callable on that surface; making them callable tools is a separate change, because it alters the surface's callable command set and therefore its published identity

#### Scenario: Withheld command degrades its action

- **WHEN** the catalog is resolved against a surface that does not export a command named in an action's `advanced` list
- **THEN** that entry is filtered from the action
- **AND** the action stays available when any route survives, and is marked unavailable when none does

### Requirement: Ask Action
The system SHALL provide an `ask` action for recall-oriented lookup over durable knowledge.

#### Scenario: Ask performs cheap recall by default
- **WHEN** a user runs the ask action with a query
- **THEN** it routes to `find` with compact lookup defaults suitable for first-pass recall
- **AND** it does not force reranking, pack assembly, or graph enrichment by default

#### Scenario: Ask can request deeper reasoning context
- **WHEN** a user runs the ask action with an explicit deep/context option
- **THEN** it routes to `find` with packed reasoning context
- **AND** graph enrichment remains explicit and soft-fails if the graph sidecar is unavailable

### Requirement: Remember And Capture Actions
The system SHALL distinguish durable conclusions from raw captured material in the simple action layer.

#### Scenario: Remember routes to compiled knowledge
- **WHEN** a user runs the remember action
- **THEN** the action routes to the compiled-note path
- **AND** the required fields and type-specific validation are the same as the canonical `note` operation

#### Scenario: Capture routes to raw material
- **WHEN** a user runs the capture action
- **THEN** the action routes to raw source or evidence preservation paths
- **AND** raw provenance is preserved rather than silently converted into a compiled conclusion

### Requirement: Review Connect Adopt And Maintain Actions
The system SHALL expose simple actions for knowledge hygiene, graph building, adoption, and maintenance while preserving existing safety contracts.

#### Scenario: Review stays proposal-oriented
- **WHEN** a user runs the review action
- **THEN** it routes to review queues or audit reports
- **AND** it does not mutate vault content unless the user explicitly selects a write-capable maintenance path

#### Scenario: Connect stays proposal-oriented by default
- **WHEN** a user runs the connect action
- **THEN** it routes to link or relation suggestion paths by default
- **AND** suggested graph relations remain review-only until accepted through an explicit write

#### Scenario: Adopt remains safe by default
- **WHEN** a user runs the adopt action
- **THEN** it uses scan-only adoption by default
- **AND** copy or compile planning modes require explicit options and preserve originals

#### Scenario: Maintain separates audit from fix
- **WHEN** a user runs the maintain action without a fix option
- **THEN** it performs read-only diagnostics
- **AND** write-capable fixes remain explicit and use the canonical `audit_fix` or reconcile paths
