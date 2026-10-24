# Spec Delta

## MODIFIED Requirements

### Requirement: Explicit idempotency remains authoritative across surfaces
The system SHALL preserve caller-supplied idempotency keys and mismatch rejection for REST and CLI callers, and SHALL accept explicit keys at the MCP adapter for mutation-capable tools. MCP explicit keys MUST use verified normalized principal identity, never raw bearer or session fallback. Existing vault and verified client isolation, credential-issuance replay exclusions and retention SHALL remain effective whether or not writer-lease coordination is enabled.

#### Scenario: Standalone REST retry with explicit key
- **WHEN** a standalone deployment receives two identical mutations with the same explicit idempotency key
- **THEN** it executes the mutation once and replays the saved result

#### Scenario: Explicit key reused with different input
- **WHEN** a caller reuses an explicit idempotency key for different canonical arguments
- **THEN** the system rejects the call with `IDEMPOTENCY_KEY_REUSED`

#### Scenario: MCP caller loses a mutation response
- **WHEN** an authenticated MCP caller repeats a mutation with the same explicit key within retention
- **THEN** the persisted result returns without another mutation
- **AND** the caller supplies the key through `tools/call` metadata under `io.exomem/idempotency_key`

#### Scenario: Another verified principal uses the same key
- **WHEN** another principal submits the same explicit key and arguments
- **THEN** that principal cannot replay the first principal's result

#### Scenario: MCP caller omits the explicit key
- **WHEN** an MCP mutation omits the explicit key
- **THEN** existing implicit replay behavior remains unchanged
