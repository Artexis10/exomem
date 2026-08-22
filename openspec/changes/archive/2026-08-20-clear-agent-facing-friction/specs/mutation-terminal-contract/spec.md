## ADDED Requirements

### Requirement: Committed Mutations Persist One Canonical Terminal

The system SHALL construct a versioned canonical terminal record after the canonical writer commits and before the idempotency receipt is marked completed. The terminal MUST contain `ok=true`, `status="committed"`, `mutated=true`, the original request identity, stable receipt identity when available, primary path/paths, and `warnings_count`, plus the complete leaf result retained for projection.

#### Scenario: Acknowledgement is lost after terminal persistence
- **WHEN** a mutation commits and deterministic fault injection interrupts delivery after the terminal is persisted
- **THEN** retrying the same identity returns the original terminal without executing the leaf again
- **AND** only one canonical write exists

#### Scenario: Retry uses a different response detail
- **WHEN** one committed identity is requested first as compact and then as full or legacy detail
- **THEN** every request resolves the same persisted terminal and original request identity
- **AND** response presentation is excluded from the mutation payload digest

### Requirement: Compact Success Is The Default Projection

Committed product mutations SHALL return a compact default projection led by `ok`, `status`, `mutated`, primary `path`, original `request_id`, stable receipt identity, and `warnings_count`. `response_detail="full"` SHALL add the complete leaf result under `diagnostics`; `response_detail="legacy"` SHALL return the pre-change raw leaf result during the compatibility window.

#### Scenario: Default committed response is decisive
- **WHEN** a governed product mutation succeeds without an explicit response detail
- **THEN** the first-level response identifies it as committed and mutated
- **AND** verbose semantic, index, warning, and transition diagnostics are not mixed into the compact terminal fields

#### Scenario: Full diagnostics are requested
- **WHEN** the same mutation is requested with `response_detail="full"`
- **THEN** the compact terminal fields are unchanged
- **AND** the complete existing leaf payload is available under `diagnostics`

### Requirement: Pre-Commit And Uncertain Errors Remain Unambiguous

The terminal projector MUST NOT convert rejected, busy, pending, committed-failure, or committed-uncertain outcomes into successful result dictionaries. `MUTATION_BUSY` SHALL remain retryable only when no commit occurred, and post-commit uncertainty SHALL retain its committed semantics and same-identity remediation.

#### Scenario: Mutation boundary is busy
- **WHEN** boundary acquisition times out before the command leaf runs
- **THEN** the call fails with structured `MUTATION_BUSY`, `status="retryable"`, and `committed=false`
- **AND** no success terminal is returned

#### Scenario: Exact post-commit terminal is unavailable
- **WHEN** canonical commit occurred but exact terminal persistence cannot be proven
- **THEN** the existing committed-uncertain error is returned
- **AND** the caller is told not to retry with a new identity

### Requirement: Legacy Receipt Rows Stay Replayable

Completed idempotency rows created before terminal versioning SHALL remain replayable for their normal bounded retention period. The system MUST return their stored legacy result without fabricating terminal fields that were never persisted.

#### Scenario: Retry reaches a pre-upgrade completed row
- **WHEN** the idempotency store contains a valid completed raw result from the previous release
- **THEN** the result replays without leaf execution or reconciliation failure
- **AND** the row expires under the existing retention policy
