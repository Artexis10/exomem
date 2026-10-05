# hosted-agent-surface Specification

## Purpose
Define the least-privilege `hosted-alpha-agent-v1` surface profile for hosted agents: one declarative membership source, a deterministic generated gateway contract, cell enforcement on authenticated private routes, and bootstrap output matching the active surface.

## Requirements

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

### Requirement: The hosted agent skill teaches the bounded episode-completeness pass

The hosted agent skill SHALL carry the same bounded episode-completeness pass the
prominence capture contract carries — in language that names no local plugin
mechanism and no tool the hosted profile does not publish, and with its classes
presented as examples rather than a closed enumeration — once its release-locked
carrier changes at a hosted candidate mint. Until that mint, the hosted bootstrap
payload SHALL carry the clause and its handling guidance, so no hosted client is
served the advisory without the doctrine for reading it.

The clause SHALL NOT be delivered by editing a shared hosted skill file that a
released identity already pins. `plugins/hosted/skills/exomem/SKILL.md` feeds the
v1 hosted release identity and the v1-v4 immutability manifest, so the carrier for
this clause is chosen by the hosted release owner rather than by an implementing
lane, and ships with whatever fresh candidate evidence the chosen route requires.

#### Scenario: Historical candidates are unchanged

- **WHEN** the clause is delivered to the hosted surface
- **THEN** the v1-v4 immutability manifest reproduces byte-for-byte and no
  historical `skills_sha256` moves

#### Scenario: A hosted client is never served the advisory without the doctrine

- **WHEN** a hosted client receives a `capture_sweep` block before the skill copy
  carries the clause
- **THEN** the bootstrap payload that client already fetched teaches how to read it

#### Scenario: The hosted skill names no unavailable mechanism

- **WHEN** the hosted skill carrying the clause is validated
- **THEN** it references no local plugin mechanism and no tool outside its
  declared required tools

### Requirement: Hosted Parity Is The Default And Exclusions Are Justified

A hosted agent profile SHALL expose the complete product command surface, including Tier-2 commands, except for commands listed in an explicit exclusion registry. Each exclusion MUST record a technical reason and the condition that lifts it. Absence of a command from a hosted profile without a recorded exclusion MUST fail the surface-contract check.

The alpha least-privilege posture of `hosted-alpha-agent-v1` is superseded: it was appropriate for a closed alpha and is not a standing reason to withhold capability from a paid tier.

#### Scenario: New product command is added

- **WHEN** a command is added to the product command registry and no hosted exclusion is recorded for it
- **THEN** the next hosted profile includes it
- **AND** a profile that omits it without a recorded exclusion fails the check rather than shipping a silent subset

#### Scenario: Exclusion is inspected

- **WHEN** the hosted exclusion registry is inspected
- **THEN** every entry names the command, a technical reason, and the condition that lifts the exclusion
- **AND** "not yet reviewed", "alpha scope", and equivalent placeholders are not accepted as reasons

#### Scenario: Tier 2 is exposed to hosted

- **WHEN** a hosted profile is resolved
- **THEN** Tier-2 commands are included unless individually excluded with a recorded reason
- **AND** each Tier-2 command remains bound by its existing guarded-field, destructive-operation, and schema-validation contracts

#### Scenario: Product action degraded by an exclusion explains itself

- **WHEN** the action catalog is resolved against a hosted profile and an action's primary route names an excluded command
- **THEN** the action is reported unavailable together with the excluded command, the recorded reason, and the condition that lifts it
- **AND** a generic "no route is exported" message is not sufficient, because it does not tell the caller whether the capability is reachable by another path
- **AND** an action whose primary route survives is reported available even when entries of its `advanced` list are withheld

### Requirement: Hosted Complete-Surface Profile

The system SHALL define the immutable profile `hosted-alpha-agent-v4` in the canonical surface-profile registry. Its membership SHALL be the complete product command surface minus the recorded exclusions, in canonical registry order, and changing membership MUST require a new profile identifier.

The profile SHALL be rendered for every supported platform channel. A profile rendered for one platform only leaves the other channel structurally behind and MUST NOT be treated as shipped.

#### Scenario: Complete-surface profile is selected

- **WHEN** a caller resolves `hosted-alpha-agent-v4`
- **THEN** the returned commands equal the product command registry minus the recorded exclusions, in canonical registry order
- **AND** every returned schema, description, route, and read/write classification comes from the corresponding canonical command entry

#### Scenario: Published profiles are unchanged

- **WHEN** `hosted-alpha-agent-v1`, `hosted-alpha-agent-v2`, or `hosted-alpha-agent-v3` is resolved after v4 is added
- **THEN** each returns its previously pinned membership in its pinned order
- **AND** their generated packages, locks, and recorded promotion evidence are byte-identical to the state before this change

#### Scenario: Profile is rendered for both platforms

- **WHEN** the v4 candidate is rendered
- **THEN** a package exists for each supported platform channel
- **AND** the platform locks agree on `command_surface_sha256`, `schema_contract_sha256`, and `compatibility_sha256`

#### Scenario: Intercepted command is excluded rather than exposed

- **WHEN** a command is intercepted by the hosted runtime and routed through a separate gateway flow
- **THEN** it is recorded as an exclusion naming that flow as the condition that lifts it
- **AND** it is not exposed as a tool that would return an interception error instead of performing work
