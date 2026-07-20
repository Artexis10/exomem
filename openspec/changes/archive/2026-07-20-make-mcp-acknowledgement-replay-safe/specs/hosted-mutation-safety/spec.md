## MODIFIED Requirements

### Requirement: Tenant-Scoped Retry And Idempotency Semantics

Hosted mutations SHALL preserve caller-supplied idempotency keys and bounded implicit MCP retry replay through the existing common invocation boundary. Retry identity MUST include the resolved tenant, stable authenticated principal scope, command, and canonical arguments, and MUST be inspected before writer authority or the mutation boundary is acquired. A key or implicit retry from one tenant or principal MUST NOT replay or suppress a mutation for another tenant or principal. Failed pre-commit mutations MUST remain retryable.

#### Scenario: Gateway retries a completed hosted mutation

- **WHEN** the gateway repeats an identical successful mutation for the same tenant and authenticated principal with the same idempotency identity
- **THEN** the original result is replayed without acquiring writer authority or executing the mutation leaf again
- **AND** only one durable vault change exists

#### Scenario: Same key is presented for another tenant or principal

- **WHEN** two tenant or principal contexts present the same explicit idempotency key for otherwise identical input
- **THEN** each context resolves an independent idempotency record
- **AND** neither context receives the other's result or suppresses the other's mutation

#### Scenario: First attempt fails before commit

- **WHEN** a mutation raises before successful completion and the caller retries it
- **THEN** the failure is not replayed as a completed result
- **AND** the retry can acquire the boundary and execute normally

#### Scenario: Identical retry arrives while original is pending

- **WHEN** the same tenant and principal repeat identical canonical arguments while the original request is still executing
- **THEN** the retry waits on the original receipt outside the mutation boundary
- **AND** it cannot return `MUTATION_BUSY` for that identity

## ADDED Requirements

### Requirement: Read-Only Maintenance Does Not Own Mutation Authority

The common invocation classifier SHALL treat `maintain_memory(mode="audit")` as read-only. It MUST NOT acquire writer authority or be assigned a mutation idempotency record.

#### Scenario: Routine audit overlaps a mutation

- **WHEN** `maintain_memory(mode="audit")` is invoked while another request holds writer authority
- **THEN** the audit is not rejected as a competing mutation
- **AND** it cannot be reported as the holder operation for `MUTATION_BUSY`
- **AND** it does not acquire the hosted consistency guard that shares the mutation boundary
