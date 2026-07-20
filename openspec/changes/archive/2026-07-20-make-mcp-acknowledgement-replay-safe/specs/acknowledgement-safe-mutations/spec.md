## ADDED Requirements

### Requirement: Mutation Identity Precedes Mutation Authority

The system SHALL reserve or inspect mutation idempotency before acquiring the vault mutation boundary or writer lease. Mutation identity MUST bind the resolved vault or tenant, authenticated principal scope, operation, and canonical payload digest.

#### Scenario: Identical retry overlaps its original

- **WHEN** a retry with the same mutation identity arrives while the original request owns the mutation boundary
- **THEN** the retry observes the original pending receipt without competing for the mutation boundary
- **AND** it never returns `MUTATION_BUSY` for that identity

#### Scenario: Same key carries a different payload

- **WHEN** a caller reuses an idempotency key for a different operation or canonical payload
- **THEN** the request fails with `IDEMPOTENCY_KEY_REUSED` before the mutation leaf runs

### Requirement: Terminal Receipts Survive Acknowledgement Loss

The system SHALL persist the exact terminal committed result before releasing mutation authority and before constructing or delivering the transport acknowledgement. Repeating the same identity SHALL return that result without rerunning the mutation.

#### Scenario: Response delivery is interrupted after commit

- **WHEN** a mutation commits and its terminal receipt is persisted but response delivery is cancelled or interrupted
- **THEN** a retry with the same identity returns the original committed result
- **AND** the mutation leaf has executed exactly once

#### Scenario: Cancellation arrives while synchronous work continues

- **WHEN** transport cancellation stops waiting for a synchronous mutation worker after canonical commit
- **THEN** the worker can finish terminal-result persistence
- **AND** a later retry retrieves that committed result without creating a duplicate

#### Scenario: Exact terminal receipt persistence fails after canonical commit

- **WHEN** a mutation crosses the canonical non-empty commit marker but its exact result cannot be serialized or stored
- **THEN** the first caller receives `MUTATION_COMMITTED_ACKNOWLEDGEMENT_UNCERTAIN`
- **AND** the receipt remains fail-closed so the same identity cannot execute again

#### Scenario: Validation result persistence fails without a commit

- **WHEN** a write-capable command returns a validation or no-op result without crossing the canonical commit marker and its receipt cannot be stored
- **THEN** the first caller receives `MUTATION_ACKNOWLEDGEMENT_PENDING` with `committed=null`
- **AND** it never claims a commit that did not occur

### Requirement: Pending And Busy States Are Unambiguous

The system SHALL use `MUTATION_BUSY` only for a request rejected before its mutation leaf runs. An identical request whose original execution is still pending SHALL bounded-wait outside the mutation boundary and, if the bound expires, SHALL report `MUTATION_ACKNOWLEDGEMENT_PENDING` rather than a pre-commit busy result.

#### Scenario: Different mutation cannot acquire the boundary

- **WHEN** a new mutation identity cannot acquire the vault mutation boundary within the configured bound
- **THEN** it returns structured `MUTATION_BUSY`
- **AND** its leaf has not run and no part of that request committed

#### Scenario: Identical request remains pending beyond the wait bound

- **WHEN** the original matching request has not persisted a terminal result before the pending wait expires
- **THEN** the retry reports `MUTATION_ACKNOWLEDGEMENT_PENDING`
- **AND** it does not claim that the mutation was rejected or safe to rerun with a new identity

### Requirement: Replay Is Isolated By Authenticated Principal

The system SHALL derive retry scope from stable verified principal claims when available. Identical public idempotency keys or payloads from different authenticated principals MUST resolve independent receipt records.

#### Scenario: Bearer rotates for the same verified principal

- **WHEN** the same verified principal retries an identical mutation using a newly issued bearer
- **THEN** the request resolves the same mutation identity and replays the original result

#### Scenario: Different principals reuse the same key

- **WHEN** two authenticated principals submit the same operation, payload, and public idempotency key
- **THEN** each principal resolves an independent mutation receipt
- **AND** neither receives the other's result

### Requirement: Terminal Receipt Retention Is Bounded

The system SHALL retain exact explicit-key terminal results for 24 hours and implicit terminal results for 60 seconds. Cleanup MUST NOT expire pending or committed-uncertain records into automatic re-execution.

#### Scenario: Exact explicit terminal result exceeds retention

- **WHEN** an explicit completed receipt is older than 24 hours
- **THEN** bounded cleanup may remove it
- **AND** later reuse is a new mutation identity outside the guarantee window

### Requirement: Acknowledgement Boundaries Are Deterministically Testable

The system SHALL provide a deterministic test seam after terminal receipt persistence and before acknowledgement return, and SHALL emit privacy-safe phase correlation for request receipt, idempotency disposition, mutation-boundary acquisition, terminal persistence, replay, and interruption.

#### Scenario: Fault injected after terminal persistence

- **WHEN** a deterministic fault interrupts the first caller immediately after its terminal receipt is durable
- **THEN** the fault does not delete or downgrade that receipt
- **AND** the next identical call returns the stored terminal result

### Requirement: Mutation-Capable POSTs Remain Single-Origin

The HA edge SHALL send MCP `tools/call`, personal REST, hosted command, lifecycle, and other mutation-capable POST requests, plus public transfer PUT uploads, to at most one origin. Timeout, cancellation, or an origin 5xx MUST NOT cause the edge to replay such a request to another replica.

#### Scenario: Active origin times out after accepting a tool call

- **WHEN** the active origin does not return before the edge bound
- **THEN** the edge returns an acknowledgement-delivery failure
- **AND** it sends no copy of that tool call to the passive origin

#### Scenario: Personal REST mutation returns 5xx or times out

- **WHEN** the selected origin accepts `/api/remember` but returns 5xx or exceeds the edge bound
- **THEN** the edge returns that failure or an acknowledgement-delivery failure
- **AND** it sends no copy of the REST POST to the passive origin

### Requirement: Correlation Identity Is Stable And Log-Safe

The system SHALL use one canonical UUIDv4 request identity from edge admission through MCP middleware, mutation reservation, canonical commit, and terminal response logging. Non-canonical caller-controlled request identifiers MUST be replaced rather than logged.

#### Scenario: Direct-origin call has no correlation header

- **WHEN** a tool call reaches the origin without `x-exomem-request-id`
- **THEN** middleware generates one canonical UUIDv4
- **AND** the bound mutation and canonical writer phases use that same UUID
