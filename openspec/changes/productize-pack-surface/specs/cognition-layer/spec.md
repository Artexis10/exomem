# cognition-layer

## MODIFIED Requirements

### Requirement: Knowledge packs are extensible schema/workflow bundles
The system SHALL support declarative knowledge packs that compose durable
primitives such as sources, compiled knowledge, entities, decisions, evidence,
records, assets, projects/cases, and review state. Packs SHALL define purpose,
audience, beginner-facing description, agent-facing instructions, default note
types, default entity types, default block types, suggested folders, suggested
workflows, routing hints, examples, and structural signals for a domain without
requiring a new storage engine or a hard-coded top-level folder per domain.

#### Scenario: Built-in packs expose product metadata
- **WHEN** the system lists available knowledge packs
- **THEN** it includes built-in packs for legal/warranty, creative, technical,
  health/athletic, business, and personal records
- **AND** each pack declares purpose, audience, beginner description, agent
  instructions, defaults, suggested folders, suggested workflows, primitives,
  actions, examples, and signals

#### Scenario: Selected packs persist as governed guidance
- **WHEN** setup or another front-door action selects one or more packs
- **THEN** Exomem records the selection under the governed Knowledge Base layer
- **AND** selection does not create suggested folders, rewrite old notes, or
  compile content by itself

#### Scenario: Custom pack validates
- **WHEN** a user or deployment adds a custom pack file
- **THEN** Exomem validates required metadata and rejects malformed packs with a
  stable error code and remediation