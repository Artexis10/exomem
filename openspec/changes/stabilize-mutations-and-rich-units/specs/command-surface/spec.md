## ADDED Requirements

### Requirement: Expected MCP Operation Failures Use Application Envelopes

The generated MCP command wrapper SHALL return deliberate public operation failures as normal tool content with top-level `success:false` and the shared stable error envelope. It MUST preserve public error details and MUST NOT expose these outcomes as MCP execution failures. Exceptions outside the deliberate public operation-error contract MUST continue through the native MCP error path.

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

### Requirement: Validation-Only Replacement Is Read-Only

`replace_memory(validate_only=true)` SHALL run as an advisory weak-snapshot preview without acquiring the writer lease, mutation boundary, or mutation idempotency receipt. It MUST identify itself as validation-only and non-committed and bind the exact draft plus relevant predecessor inputs. The eventual non-preview replacement MUST remain a mutation and MUST freshly revalidate its predecessor, draft, writer, and corpus-dependent preconditions under mutation authority.

#### Scenario: Replacement preview overlaps another writer

- **WHEN** another process holds the vault mutation boundary and `replace_memory(validate_only=true)` is invoked
- **THEN** the preview returns its advisory hash-bound proposal without `MUTATION_BUSY`
- **AND** no mutation receipt or vault write is created

#### Scenario: Replacement commit still serializes

- **WHEN** the same replacement is invoked without validation-only mode
- **THEN** it acquires the normal writer lease and process-safe boundary before committing
