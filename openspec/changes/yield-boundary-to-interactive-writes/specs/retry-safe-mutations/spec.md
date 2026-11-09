## MODIFIED Requirements

### Requirement: Prompt MCP retries replay successful mutations
The system SHALL detect a successful mutation repeated through MCP by the same authenticated principal with the same command and canonical arguments within a bounded retry window, and SHALL return the original result without executing the mutation leaf again. Replay SHALL resolve the persisted terminal even when the original call's derived-state acknowledgement failed after the commit, because the terminal is persisted before that acknowledgement. A replayed reply SHALL carry `replayed: true` in the compact projection and in `full`, and its `mutated`, `request_id` and `receipt_id` SHALL be those of the commit it replays, so the reply says the commit happened earlier. A reply that is not a replay SHALL carry no `replayed` key.

#### Scenario: Lost save acknowledgement is retried
- **WHEN** an authenticated MCP client repeats an identical successful additive save within the retry window
- **THEN** the system returns the first save result and creates only one vault artifact
- **AND** the reply carries `replayed: true` and the receipt of the first save

#### Scenario: Lost delete acknowledgement is retried
- **WHEN** an authenticated MCP client repeats an identical successful delete within the retry window
- **THEN** the system returns the first delete result instead of executing a second delete that reports `NOT_FOUND`

#### Scenario: Artifact batch acknowledgement failed after commit
- **WHEN** an artifact preservation commits, its derived-state acknowledgement fails, and the client repeats the identical call within the retry window
- **THEN** the system returns the persisted batch terminal with `derived_sync="pending"` rather than `MUTATION_OUTCOME_UNKNOWN`
- **AND** no file is fetched or written again

### Requirement: Implicit retry detection is narrowly scoped and bounded
The system MUST scope implicit retry detection to the authenticated caller when available, MUST compare canonical command arguments, and MUST expire implicit completed entries after 600 seconds. Canonical arguments SHALL be the schema-validated arguments with defaults applied, with every string in Unicode NFC and with sorted keys, so one text composed in two ways is one request. The digest SHALL NOT depend on vault state. Calls from another authenticated principal or after the window SHALL execute normally.

#### Scenario: Different principals submit identical mutations
- **WHEN** two authenticated principals submit the same command and arguments within 600 seconds
- **THEN** neither principal's call suppresses the other's mutation

#### Scenario: Intentional later repetition
- **WHEN** the same principal repeats identical mutation arguments after 600 seconds
- **THEN** the mutation leaf executes again

#### Scenario: The same text in another Unicode composition
- **WHEN** an authenticated MCP client repeats a successful write whose text differs only in Unicode composition (a precomposed letter and a base letter with a combining mark)
- **THEN** the system replays the first result and executes the mutation leaf once
