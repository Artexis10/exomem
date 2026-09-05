## ADDED Requirements

### Requirement: Compact mutation terminals surface unknown relation guidance

Every semantic mutation route that commits one or more explicit unregistered relation observations SHALL carry one bounded `relation_advisory` through the shared mutation terminal at default compact detail. The advisory SHALL name the normalized raw labels, registry hash, bounded occurrence evidence available without a hot-path corpus scan, and the read-only relation-resolution route. It SHALL state that the observation remains preserved but untraversed. The advisory MUST NOT block or rewrite the committed note, infer a parent or meaning, trigger a registry save, run an embedding search, or scan the vault synchronously.

#### Scenario: Unknown relation is visible after an ordinary write
- **WHEN** `remember`, `observe_memory`, `edit_memory`, or `replace_memory` commits an explicit `applies_to` observation that is not registered
- **THEN** the successful compact terminal contains a bounded relation advisory and resolution next action
- **AND** the raw edge remains unregistered until a separate reviewed registry save

#### Scenario: Portable and honest relations do not create bureaucracy
- **WHEN** a committed write uses an active registered relation, the legitimate `relates_to` fallback, or authors no edge
- **THEN** no unknown-relation advisory is added solely to encourage greater specificity

#### Scenario: Advisory projection is uniform across public surfaces
- **WHEN** equivalent semantic mutations are committed through MCP, REST, and CLI
- **THEN** the same shared terminal projection exposes or omits the relation advisory under the same conditions
- **AND** no facade performs local relation reasoning
