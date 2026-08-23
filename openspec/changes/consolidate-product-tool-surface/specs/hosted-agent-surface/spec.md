## ADDED Requirements

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

#### Scenario: Product actions all resolve on hosted

- **WHEN** the action catalog is resolved against a hosted profile
- **THEN** no action is reported unavailable
- **AND** an action reported unavailable identifies the excluded command and its recorded reason

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
