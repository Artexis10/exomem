## ADDED Requirements

### Requirement: Hosted Mutation Error Shapes Cover Every Terminal Code
The hosted response layer SHALL carry the structured `status` and `committed` fields for every mutation error code the mutation terminal can emit, including `MUTATION_OUTCOME_UNKNOWN` (`status="uncertain"`, `committed=null`), so a connector client can distinguish never-executed, retryable, committed, and uncertain outcomes without parsing prose.

#### Scenario: Outcome-unknown reaches the hosted client
- **WHEN** a hosted mutation resolves to `MUTATION_OUTCOME_UNKNOWN`
- **THEN** the hosted error carries `status="uncertain"`, `committed=null`, and the same-identity remediation text
- **AND** the ledger row records the code as a refusal

#### Scenario: Shape table and terminal codes stay aligned
- **WHEN** the test suite enumerates the mutation terminal's error codes that carry `status` details
- **THEN** every such code has an entry in the hosted shape table
