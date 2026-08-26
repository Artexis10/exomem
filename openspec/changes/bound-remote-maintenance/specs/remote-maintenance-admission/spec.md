## Purpose

Keep request-bound remote clients responsive by separating bounded maintenance
inspection from potentially long-running operator repair.

## ADDED Requirements

### Requirement: Remote maintenance writes fail before vault admission

The system SHALL reject a write-mode `maintain_memory` invocation from an MCP,
REST, or hosted request before it acquires writer authority, idempotency state, or
the vault mutation boundary. The refusal SHALL use stable code
`MAINTENANCE_REQUIRES_CLI`, SHALL identify the invocation as not committed, and
SHALL direct the caller to the corresponding local operator CLI command.

#### Scenario: Remote reconcile is refused immediately
- **WHEN** an MCP, REST, or hosted caller requests `mode="reconcile"` without `dry_run=true`
- **THEN** the system returns `MAINTENANCE_REQUIRES_CLI` before writer-manager dispatch
- **AND** no vault mutation boundary is acquired

#### Scenario: Explicit remote repair is refused immediately
- **WHEN** an MCP, REST, or hosted caller requests `mode="fix"` or `mode="backfill-ids"` with `dry_run=false`
- **THEN** the system returns the same non-committed operator-CLI refusal before writer-manager dispatch

### Requirement: Remote inspection and local maintenance remain available

The system SHALL continue to admit read-only maintenance audit and explicit dry
runs on remote surfaces. It SHALL continue to admit write-mode maintenance from
the local CLI and direct trusted Python integrations that have no request-bound
remote surface descriptor.

#### Scenario: Remote maintenance inspection remains callable
- **WHEN** an MCP, REST, or hosted caller requests audit or an explicitly read-only maintenance preview
- **THEN** the canonical maintenance leaf executes through the ordinary read-only admission path

#### Scenario: Local operator repair remains callable
- **WHEN** the local CLI requests a write-mode maintenance operation
- **THEN** the canonical maintenance leaf proceeds through ordinary writer and mutation admission

### Requirement: Tool guidance prevents unsafe remote routing

The generated `maintain_memory` contract and installed generic Exomem guidance
SHALL tell agents that remote use is limited to audit and dry runs and that actual
repair or reconcile belongs to the local operator CLI.

#### Scenario: Agent discovers maintenance restrictions
- **WHEN** an agent reads the generated tool description or installed operation guidance
- **THEN** it can distinguish safe remote inspection from local operator repair without first attempting a write
