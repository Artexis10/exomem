## ADDED Requirements

### Requirement: A custody Source waits in the projection, never in default attention

The due-state projection SHALL include `custody_source`, due from the Source's capture date, refreshed and reconciled like the Source backlog it shares a pass with. The family SHALL be registered as an attention category so it is listable by name, and SHALL NOT join the default attention set. In the backlog order it SHALL sort after every other backlog family, because it waits by choice.

#### Scenario: A custody Source is counted, never pushed

- **WHEN** a Source marked `custody_only: true` has no live page compiling it
- **THEN** the served projection counts it under `custody_source`, listed after `unprocessed_source` and the episode backlog
- **AND** the default attention view does not surface it

#### Scenario: Compiling a custody Source updates the projection without a reconcile

- **WHEN** a live note citing a custody Source is created
- **THEN** the next served projection no longer counts that Source, with no reconcile run in between
