## ADDED Requirements

### Requirement: Versioned agent commands bind the expected contract at execution

The hosted cell SHALL expose the additive private agent command route defined by `contracts/hosted-agent-command-binding-v1.json`. It MUST authenticate its unique service credential and cell/principal context, then validate the expected profile, runtime release, protocol, command fingerprint and published schema digest against its running registered agent contract before resolving a vault path or executing a command. Catalog/plugin compatibility MUST remain verified by the gateway's approved candidate/fleet binding and MUST NOT be falsely represented as recomputable from the cell runtime. The public MCP route/version is unchanged.

#### Scenario: Expected contract matches the running cell

- **WHEN** an authenticated v2 agent command carries the complete matching expected tuple
- **THEN** the cell dispatches through its normal registry and lifecycle/mutation boundary without requiring an earlier contract request

#### Scenario: Expected context is missing or mismatched

- **WHEN** a v2 invocation omits, duplicates, malforms or mismatches any required expected-contract field
- **THEN** it returns the specified stable content-free compatibility error before command execution or vault access
- **AND** no legacy route is invoked as fallback

#### Scenario: Runtime changes between requests

- **WHEN** a gateway sends a tuple belonging to an earlier process contract after the cell runtime changes
- **THEN** the current cell rejects it before executing the command
- **AND** a successful earlier contract GET grants no exception

### Requirement: Additive command binding preserves existing command safety

Both private agent route versions SHALL use the same registry leaves, governed errors, lifecycle admission and tenant/principal idempotency namespace. Older approved gateways MUST retain the existing v1 route during the documented rollout window. Advertising command-time binding MUST be part of the signed compatibility contract.

#### Scenario: Mutation acknowledgement is lost during gateway rollout

- **WHEN** the same authenticated principal and cell retry a mutation with identical canonical input and idempotency key through a supported adapter
- **THEN** the existing committed result is replayed without a second mutation regardless of private route version
