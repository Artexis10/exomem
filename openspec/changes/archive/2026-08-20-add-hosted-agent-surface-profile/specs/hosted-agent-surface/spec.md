## ADDED Requirements

### Requirement: Hosted Alpha Agent Profile Is Explicit And Least Privilege

The system SHALL define the immutable profile `hosted-alpha-agent-v1` in a canonical surface-profile registry beside the product command registry. The profile MUST contain exactly `bootstrap`, `ask_memory`, `read_memory`, `browse_memory`, `remember`, `observe_memory`, `capture_source`, `compile_source`, `preserve_evidence`, `review_memory`, `review_item_context`, `triage_memory`, and `connect_memory`, in its pinned order. Changing membership MUST require a new profile identifier.

#### Scenario: Alpha profile is selected

- **WHEN** a caller resolves `hosted-alpha-agent-v1` for the REST-backed Hosted agent surface
- **THEN** the resolver returns exactly the thirteen named Tier-1 product commands in canonical registry order
- **AND** every returned schema, description, route, and read/write classification comes from the corresponding canonical command entry

#### Scenario: Broad operations are not exposed

- **WHEN** the alpha profile is inspected
- **THEN** it excludes broad page-level editing and replacement, coordination internals, transfer, media processing, adoption, maintenance, schema administration, and every Tier-2 command
- **AND** those exclusions cannot be bypassed by selecting another surface or enabling Tier-2

#### Scenario: Governance files cannot be rewritten through broad page mutation

- **WHEN** an agent requests `edit_memory` or `replace_memory`, including with a path under `_Schema`
- **THEN** the command is absent from the profile and rejected before invocation or lifecycle admission
- **AND** governed semantic-unit mutation remains available through `observe_memory`

### Requirement: Profile Membership Has One Declarative Source

Profile membership SHALL be one immutable, ordered set-level policy in the canonical surface-profile registry. Gateway/transport code MUST NOT maintain another command-name allowlist or copied command schema for the Hosted agent surface, and an unknown profile identifier MUST fail closed.

#### Scenario: Canonical command metadata changes

- **WHEN** an included command's canonical parameter schema, description, route metadata, or read/write classification changes
- **THEN** the next derived agent contract reflects that canonical change without a parallel schema edit

#### Scenario: Unknown profile is requested

- **WHEN** a caller requests an unregistered Hosted agent profile
- **THEN** profile resolution fails with a stable error before returning any command contract

### Requirement: Agent Gateway Contract Is Deterministic And Additive

The system SHALL generate a deterministic Hosted agent gateway contract from the selected profile using the existing envelope contract, protocol compatibility policy, and canonical JSON digest. The contract SHALL include the profile identifier, exact active capability metadata, active capability fingerprint, full schema-contract digest, and canonical MCP discovery description, input JSON Schema, and annotations for every command. It MUST omit the unrelated private transfer-grant capability. The existing full private gateway contract MUST retain its current default shape, command set, and digest behavior.

#### Scenario: Agent contract is generated twice

- **WHEN** `hosted-alpha-agent-v1` is generated twice for the same release and protocol
- **THEN** both canonical JSON payloads and digests are identical
- **AND** the command list equals the resolved profile in the same order

#### Scenario: Shared command is compared with the private contract

- **WHEN** an agent-profile command is looked up in the full private contract
- **THEN** its serialized command entry is identical in both contracts
- **AND** the profile contract contains no command absent from the full private contract

#### Scenario: Existing private contract is generated

- **WHEN** the existing private gateway contract builder is called without an agent profile
- **THEN** it returns the complete registry-derived private contract without agent-profile metadata
- **AND** existing control-plane consumers do not need to opt into or understand the new profile

### Requirement: Cell Enforces The Agent Profile On Authenticated Private Routes

The cell SHALL expose authenticated profile-specific private contract and command routes without changing the existing full private routes. The agent command route MUST resolve commands only from the pinned profile, bind that profile's active surface descriptor during invocation, and reject an excluded or unknown command before leaf invocation or lifecycle admission. Profile selection MUST NOT be accepted from a public caller-controlled body, query, cookie, or untrusted header.

#### Scenario: Trusted gateway invokes an allowed agent command

- **WHEN** the authenticated control plane forwards a command to the private `hosted-alpha-agent-v1` agent route with valid trusted cell and principal context
- **THEN** the cell resolves the command from that profile, binds the matching active descriptor, and invokes the canonical command through normal admission and idempotency handling

#### Scenario: Trusted gateway invokes an excluded command

- **WHEN** the authenticated control plane names transfer, adoption, media, maintenance, schema, coordination, Tier-2, or another command absent from `hosted-alpha-agent-v1`
- **THEN** the cell returns `COMMAND_NOT_FOUND` before invoking a leaf or entering lifecycle admission
- **AND** it does not fall back to the full private command router

#### Scenario: Existing private command route is used

- **WHEN** an existing Home or control-plane caller uses the legacy private command or contract route
- **THEN** the complete existing private command surface and `private-command-router` descriptor remain available
- **AND** its default contract shape and digest behavior are unchanged

### Requirement: Bootstrap Matches The Active Hosted Agent Surface

The system SHALL construct one active surface descriptor from `hosted-alpha-agent-v1` and SHALL use the same descriptor metadata and fingerprint in the derived contract and bootstrap context. For `compact`, `full`, and `diagnostics`, bootstrap MUST NOT advertise a product tool, callable route, example, or workflow step unavailable on the active profile.

#### Scenario: Generic client bootstraps through the Hosted agent surface

- **WHEN** `bootstrap` runs with the `hosted-alpha-agent-v1` active descriptor
- **THEN** `active_capabilities` identifies that profile, disables Tier-2, lists exactly the profile commands, and carries the same fingerprint as the derived agent contract
- **AND** every callable reference in the bootstrap payload belongs to the active descriptor

#### Scenario: Excluded workflow guidance is filtered

- **WHEN** bootstrap guidance would normally mention transfer, adoption, media, maintenance, schema, or Tier-2 operations
- **THEN** the unavailable tool reference, route, example, or workflow entry is removed or marked unavailable
- **AND** capture, recall, review, and connection guidance that remains executable is preserved
