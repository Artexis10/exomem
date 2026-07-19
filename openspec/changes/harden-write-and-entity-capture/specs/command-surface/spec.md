## ADDED Requirements

### Requirement: Validate-Only Edit Is Classified Read-Only
`invocation_is_read_only` SHALL classify `edit_memory` as read-only only when `validate_only` is exactly true. That invocation SHALL run structural and semantic validation without writer-lease acquisition, idempotency receipt creation, or entry into the vault mutation boundary. Every non-validate edit invocation SHALL remain a mutation.

#### Scenario: Validate-only edit overlaps a live mutation
- **WHEN** `edit_memory(validate_only=true)` runs while another operation owns the vault mutation boundary
- **THEN** validation reads guarded current state without returning `MUTATION_BUSY` solely because of that owner
- **AND** it creates no canonical, index, log, receipt, or sidecar mutation

#### Scenario: Ordinary edit remains guarded
- **WHEN** `edit_memory` omits `validate_only` or sets it false
- **THEN** the command is classified as a mutation and uses normal writer, idempotency, and vault-boundary safeguards

### Requirement: Entity Surfaces Derive From The Active Registry
The MCP, REST, CLI, OpenAPI, and bootstrap surfaces SHALL expose entity kinds and guidance derived from the same active registry used by the canonical entity leaf. A registered kind MUST NOT require an independently edited command description or hard-coded validation tuple, and a kind absent from the registry MUST be rejected consistently on every surface.

#### Scenario: Organization is visible everywhere
- **WHEN** the built-in registry contains `organization`
- **THEN** bootstrap and generated entity guidance list it, and MCP, REST, and CLI all accept it through the same leaf

#### Scenario: Surface drift gate runs
- **WHEN** tests compare registry definitions with generated schemas, bootstrap catalogs, scaffold registry documentation, and entity indexes
- **THEN** the build fails if a supported kind is missing or independently enumerated on a required surface
