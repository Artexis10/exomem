## ADDED Requirements

### Requirement: Prompt MCP retries replay successful mutations
The system SHALL detect a successful mutation repeated through MCP by the same authenticated principal with the same command and canonical arguments within a bounded retry window, and SHALL return the original result without executing the mutation leaf again.

#### Scenario: Lost save acknowledgement is retried
- **WHEN** an authenticated MCP client repeats an identical successful additive save within the retry window
- **THEN** the system returns the first save result and creates only one vault artifact

#### Scenario: Lost delete acknowledgement is retried
- **WHEN** an authenticated MCP client repeats an identical successful delete within the retry window
- **THEN** the system returns the first delete result instead of executing a second delete that reports `NOT_FOUND`

### Requirement: Implicit retry detection is narrowly scoped and bounded
The system MUST scope implicit retry detection to the authenticated caller when available, MUST compare canonical command arguments, and MUST expire implicit completed entries after 60 seconds. Calls from another authenticated principal or after the window SHALL execute normally.

#### Scenario: Different principals submit identical mutations
- **WHEN** two authenticated principals submit the same command and arguments within 60 seconds
- **THEN** neither principal's call suppresses the other's mutation

#### Scenario: Intentional later repetition
- **WHEN** the same principal repeats identical mutation arguments after 60 seconds
- **THEN** the mutation leaf executes again

### Requirement: Failed mutations remain retryable
The system SHALL NOT cache a failed mutation as a successful replay result and SHALL remove its pending implicit entry so a later identical call can execute.

#### Scenario: First attempt fails before completion
- **WHEN** an implicitly keyed MCP mutation raises an error and the client retries it
- **THEN** the retry executes the mutation leaf instead of replaying the error

### Requirement: Explicit idempotency remains authoritative across surfaces
The system SHALL preserve durable caller-supplied idempotency keys and mismatch rejection for REST and CLI callers, and SHALL honor them whether or not writer-lease coordination is enabled.

#### Scenario: Standalone REST retry with explicit key
- **WHEN** a standalone deployment receives two identical mutations with the same explicit idempotency key
- **THEN** it executes the mutation once and replays the saved result

#### Scenario: Explicit key reused with different input
- **WHEN** a caller reuses an explicit idempotency key for different canonical arguments
- **THEN** the system rejects the call with `IDEMPOTENCY_KEY_REUSED`
