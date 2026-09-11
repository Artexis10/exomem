## MODIFIED Requirements

### Requirement: Prompt MCP retries replay successful mutations
The system SHALL detect a successful mutation repeated through MCP by the same authenticated principal with the same command and canonical arguments within a bounded retry window, and SHALL return the original result without executing the mutation leaf again. Replay SHALL resolve the persisted terminal even when the original call's derived-state acknowledgement failed after the commit, because the terminal is persisted before that acknowledgement.

#### Scenario: Lost save acknowledgement is retried
- **WHEN** an authenticated MCP client repeats an identical successful additive save within the retry window
- **THEN** the system returns the first save result and creates only one vault artifact

#### Scenario: Lost delete acknowledgement is retried
- **WHEN** an authenticated MCP client repeats an identical successful delete within the retry window
- **THEN** the system returns the first delete result instead of executing a second delete that reports `NOT_FOUND`

#### Scenario: Artifact batch acknowledgement failed after commit
- **WHEN** an artifact preservation commits, its derived-state acknowledgement fails, and the client repeats the identical call within the retry window
- **THEN** the system returns the persisted batch terminal with `derived_sync="pending"` rather than `MUTATION_OUTCOME_UNKNOWN`
- **AND** no file is fetched or written again
