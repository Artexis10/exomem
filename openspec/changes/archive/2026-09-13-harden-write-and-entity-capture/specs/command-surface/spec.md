## ADDED Requirements

### Requirement: Validate-Only Edit Is Classified Read-Only
`invocation_is_read_only` SHALL classify `edit_memory` as read-only only when `validate_only` is exactly true. That invocation SHALL run structural and semantic validation without writer-lease acquisition, idempotency receipt creation, or entry into the vault mutation boundary. Every non-validate edit invocation SHALL remain a mutation.

`edit_memory` SHALL expose the transition token and reviewed-relation fields returned by validate-only semantic preflight so a client can commit the exact reviewed transition without bypassing the semantic contract.

#### Scenario: Validate-only edit overlaps a live mutation
- **WHEN** `edit_memory(validate_only=true)` runs while another operation owns the vault mutation boundary
- **THEN** validation reads guarded current state without returning `MUTATION_BUSY` solely because of that owner
- **AND** it creates no canonical, index, log, receipt, or sidecar mutation

#### Scenario: Ordinary edit remains guarded
- **WHEN** `edit_memory` omits `validate_only` or sets it false
- **THEN** the command is classified as a mutation and uses normal writer, idempotency, and vault-boundary safeguards
