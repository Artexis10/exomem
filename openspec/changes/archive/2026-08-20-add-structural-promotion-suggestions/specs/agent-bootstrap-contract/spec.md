## ADDED Requirements

### Requirement: The agent contract teaches structural-promotion presentation

Bootstrap and the canonical write loop SHALL teach agents that a compiled write may return one advisory `structure_suggestion`, and SHALL state how to act on it.

The guidance SHALL direct agents to normally surface a `strong` suggestion, to describe it in the user's own domain language rather than in Exomem's internal terms, to ask before reorganising unless the user has explicitly delegated curation, to prefer an existing suitable destination over creating a new one, to avoid repeating the same recommendation within one interaction, and to exercise judgement on a `moderate` suggestion where mentioning it would be bureaucracy rather than help.

The guidance SHALL state that the suggestion is advisory and that the runtime never reorganises knowledge on its own.

#### Scenario: Bootstrap names the signal and the expected behaviour

- **WHEN** a generic client reads the bootstrap post-write guidance
- **THEN** it learns that a durable write may return a structural suggestion and where to find it
- **AND** it learns to surface a strong suggestion, to ask before restructuring, and to prefer an existing destination

#### Scenario: Guidance is presentational, not executable

- **WHEN** bootstrap describes structural suggestions
- **THEN** reading bootstrap creates, moves, or reorganises nothing
- **AND** no new tool or parameter is required to receive the suggestion

### Requirement: Post-write guidance names only fields the response carries

The write-loop guidance SHALL describe the fields a successful mutation actually returns at its default response detail, and SHALL identify the response detail required for any field it names that is not returned by default.

#### Scenario: The write loop does not direct agents to absent fields

- **WHEN** the canonical write loop tells an agent to inspect the result of a durable write
- **THEN** every field it names is either present in the default committed response
- **AND** or is explicitly marked as requiring a higher response detail
