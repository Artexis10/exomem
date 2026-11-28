## MODIFIED Requirements

### Requirement: Prompt MCP retries replay successful mutations
The system SHALL detect a successful mutation repeated through MCP by the same authenticated principal with the same command and canonical arguments within a bounded retry window. It SHALL resolve the completed write without executing the mutation leaf again. Replay SHALL resolve the persisted terminal even when the original call's derived-state acknowledgement failed after the commit.

The compact and full projections of a replayed terminal SHALL carry `replayed: true`. When a replay returns the stored terminal, it SHALL retain the stored `mutated`, `request_id` and `receipt_id`. A replay terminal built from a verified receipt SHALL report `status="replayed"` and `mutated=false` and retain original identities when available. An original terminal SHALL omit the replay marker. Legacy detail for a versioned terminal SHALL return the compatibility leaf without adding the terminal's replay marker. Pre-versioned results remain replayable under the mutation-terminal contract.

#### Scenario: Lost save acknowledgement is retried
- **WHEN** an authenticated MCP client repeats an identical successful additive save within the retry window using compact or full detail
- **THEN** the system resolves the completed save and creates only one vault artifact
- **AND** the projection carries `replayed: true` and full detail provides the original receipt identity when available

#### Scenario: Lost delete acknowledgement is retried
- **WHEN** an authenticated MCP client repeats an identical successful delete within the retry window
- **THEN** the system resolves the completed delete without executing the mutation leaf again

#### Scenario: Artifact batch acknowledgement failed after commit
- **WHEN** an artifact preservation commits, its derived-state acknowledgement fails, and the client repeats the identical call within the retry window
- **THEN** the system returns the persisted batch terminal with `derived_sync="pending"` rather than `MUTATION_OUTCOME_UNKNOWN`
- **AND** no file is fetched or written again

#### Scenario: A verified receipt supplies a replay terminal
- **WHEN** the system builds a replay terminal from a verified receipt
- **THEN** the terminal records `status="replayed"`, `mutated=false`, `replayed: true` and the original identities available for the commit
- **AND** compact and full show the marker and full detail shows the operational fields in `diagnostics.operational`

#### Scenario: A client requests legacy detail for a replayed terminal
- **WHEN** the client retries a completed versioned mutation with `response_detail="legacy"`
- **THEN** the system returns the compatibility leaf without adding the terminal's replay marker
- **AND** the mutation leaf does not execute again

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
