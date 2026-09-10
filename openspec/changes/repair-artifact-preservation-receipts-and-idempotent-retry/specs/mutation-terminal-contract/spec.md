## MODIFIED Requirements

### Requirement: Committed Mutations Persist One Canonical Terminal
The system SHALL construct a versioned canonical terminal record after the canonical writer commits and before the idempotency receipt is marked completed. The terminal MUST contain `ok=true`, `status="committed"`, `mutated=true`, the original request identity, stable receipt identity when available, primary path/paths, and `warnings_count`, plus the complete leaf result retained for projection. The terminal SHALL be persisted before any derived-state acknowledgement (index upsert, graph refresh, advisory projection) runs for the commit. When that acknowledgement fails or exceeds its budget, the persisted terminal SHALL remain a success terminal whose `derived_sync` reads `"pending"` and whose warnings name the component class without a path; the failure MUST NOT be reported as a committed-uncertain error.

#### Scenario: Acknowledgement is lost after terminal persistence
- **WHEN** a mutation commits and deterministic fault injection interrupts delivery after the terminal is persisted
- **THEN** retrying the same identity returns the original terminal without executing the leaf again
- **AND** only one canonical write exists

#### Scenario: Retry uses a different response detail
- **WHEN** one committed identity is requested first as compact and then as full or legacy detail
- **THEN** every request resolves the same persisted terminal and original request identity
- **AND** response presentation is excluded from the mutation payload digest

#### Scenario: Derived acknowledgement fails after the commit
- **WHEN** a mutation commits and deterministic fault injection makes the derived-state acknowledgement raise or exceed its budget
- **THEN** the call returns a success terminal with `status="committed"`, `derived_sync="pending"` and a warning naming the failed component class
- **AND** the terminal is already persisted, so a same-identity retry replays it without executing the leaf again

### Requirement: Pre-Commit And Uncertain Errors Remain Unambiguous
The terminal projector MUST NOT convert rejected, busy, pending, committed-failure, or committed-uncertain outcomes into successful result dictionaries. `MUTATION_BUSY` SHALL remain retryable only when no commit occurred, and post-commit uncertainty SHALL retain its committed semantics and same-identity remediation. Committed-uncertain SHALL be returned only when the canonical commit is proven and persisting the terminal record itself failed; a failure in derived-state acknowledgement after a persisted terminal is not uncertainty and SHALL NOT produce this error.

#### Scenario: Mutation boundary is busy
- **WHEN** boundary acquisition times out before the command leaf runs
- **THEN** the call fails with structured `MUTATION_BUSY`, `status="retryable"`, and `committed=false`
- **AND** no success terminal is returned

#### Scenario: Exact post-commit terminal is unavailable
- **WHEN** canonical commit occurred but exact terminal persistence cannot be proven
- **THEN** the existing committed-uncertain error is returned
- **AND** the caller is told not to retry with a new identity

#### Scenario: Terminal persistence fails but acknowledgement would have succeeded
- **WHEN** canonical commit occurred and deterministic fault injection makes terminal persistence fail
- **THEN** the call returns committed-uncertain with `status="committed"` and `committed=true`
- **AND** a same-identity retry resolves the canonical row rather than executing the leaf again
