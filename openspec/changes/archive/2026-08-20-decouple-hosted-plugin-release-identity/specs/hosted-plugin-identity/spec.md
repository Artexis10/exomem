## ADDED Requirements

### Requirement: Descriptor identity tracks the contract surface
The system SHALL derive the hosted plugin compatibility descriptor and its
`compatibility_sha256` from the plugin contract surface, without including the
Exomem release.

#### Scenario: Release bump with no contract change
- **WHEN** the Exomem package version changes and no command, schema, capability, skill, or endpoint changes
- **THEN** the generated compatibility descriptor is byte-identical to the committed one
- **AND** `compatibility_sha256` is unchanged

#### Scenario: Contract surface change
- **WHEN** a command, schema digest, capability surface, skill dependency, or endpoint changes
- **THEN** `compatibility_sha256` changes

#### Scenario: Descriptor content
- **WHEN** the compatibility descriptor is generated
- **THEN** it does not contain the Exomem release under any key

### Requirement: Hosted definition carries no Exomem release
The system SHALL define the hosted plugin without pinning the Exomem release.

#### Scenario: Definition schema
- **WHEN** the hosted plugin definition is loaded
- **THEN** `source_release` is neither required nor read

#### Scenario: Regeneration after a version bump
- **WHEN** `hosted-plugin.py regenerate` runs after an Exomem version bump
- **THEN** it completes without requiring any prior resynchronization of the definition

### Requirement: Runtime release reporting is preserved
The system SHALL continue to report the running Exomem release in the agent
gateway contract.

#### Scenario: Runtime contract
- **WHEN** the agent gateway contract is built
- **THEN** it reports `exomem_release` for the running package

### Requirement: Promotion identity is unaffected by release
The system SHALL bind promotion evidence to the contract and package identity
rather than to the Exomem release.

#### Scenario: Live record after an unrelated release
- **WHEN** an Exomem release ships that changes no contract surface
- **THEN** an existing live promotion record remains valid
- **AND** no re-promotion is required
