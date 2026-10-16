## MODIFIED Requirements

### Requirement: Expected MCP Operation Failures Use Application Envelopes

The generated MCP command wrapper SHALL return deliberate public operation failures as normal tool content with top-level `success:false` and the shared stable error envelope. A deliberate public operation failure is any coded leaf refusal: an `OpError`, a semantic refusal on the exception chain, an error that carries its own public outcome, and an error whose text follows the `CODE: reason` convention. It MUST preserve public error details and MUST NOT expose these outcomes as MCP execution failures. Exceptions outside the deliberate public operation-error contract MUST continue through the native MCP error path. A lost or unclassified authorization context MUST stay the content-free native tool error that the authorization middleware raises.

#### Scenario: Busy mutation is retryable application data

- **WHEN** a mutation raises public `MUTATION_BUSY` before commit
- **THEN** the MCP tool result is not protocol `isError`
- **AND** its content reports `success:false`, code `MUTATION_BUSY`, `status:retryable`, `committed:false`, retry guidance, request ID, and receipt ID

#### Scenario: Read remains callable after repeated busy outcomes

- **WHEN** the same MCP effective retry scope and idempotency store receive repeated structured busy outcomes for the same canonical command payload
- **THEN** a subsequent read-only command can execute normally
- **AND** retrying the original mutation with the same identity cannot create a duplicate commit

#### Scenario: Receipt is not a cross-session replay key

- **WHEN** a caller starts a new session without a transferable explicit idempotency identity
- **THEN** the prior receipt remains diagnostic rather than caller-supplied replay authority
- **AND** the client follows reconciliation guidance instead of assuming cross-session duplicate suppression

#### Scenario: Unexpected exception remains a tool failure

- **WHEN** a command raises an unexpected exception that is not a public operation error
- **THEN** the wrapper preserves FastMCP's native execution-error behavior

#### Scenario: A coded leaf refusal answers with the error envelope, not a tool error

- **WHEN** a leaf refuses with a stable code, such as `STRING_NOT_FOUND` re-wrapped as `CODE: reason`
- **THEN** the MCP tool result is not protocol `isError`
- **AND** its content reports `success:false` and that code

#### Scenario: A committed batch failure keeps its outcome

- **WHEN** a mutation raises `BATCH_CLEANUP_INCOMPLETE` after its destinations are committed
- **THEN** the MCP tool result reports `success:false` with the code, the committed outcome, and the do-not-retry remediation
- **AND** an identical retry replays the same envelope without a second leaf call

#### Scenario: Lost authorization stays a native tool error

- **WHEN** a command raises `AuthorizationContextUnavailable` inside the wrapper
- **THEN** the wrapper re-raises it, and the client receives the content-free native tool error
