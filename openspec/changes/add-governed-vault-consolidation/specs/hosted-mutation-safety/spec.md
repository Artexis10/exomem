## ADDED Requirements

### Requirement: Limited owner connectors retain allowed write authority

Connector ceilings SHALL preserve owner identity and legitimate writes to admitted content. Hidden-target admission SHALL precede existence, collision, and parsing observations. Moves, replacement, and reclassification SHALL check both current and proposed membership. These checks SHALL NOT introduce routine human approval or a blanket read-only owner role.

#### Scenario: An allowed owner edit succeeds

- **WHEN** a limited owner edits admitted content without changing protective membership
- **THEN** the existing governed mutation completes under ordinary owner authority
- **AND** no additional approval queue is introduced

#### Scenario: A hidden target is addressed for mutation

- **WHEN** a limited connector attempts to edit, replace, move, or reclassify a hidden target
- **THEN** admission refuses before exposing stored existence, conflict, or parse facts
- **AND** the operation changes no canonical content

#### Scenario: A write would escape protective membership

- **WHEN** a limited connector proposes a move, replacement, or metadata change that removes protective membership
- **THEN** before-and-after admission refuses the self-widening effect
- **AND** a normal allowed write remains available

### Requirement: Connectors cannot mutate their protective configuration

Host-owned connector configuration and referenced protective Scope definitions SHALL remain outside limited connectors' mutation authority. Connector names, projects, and Scope IDs SHALL remain configuration data. The system SHALL reuse canonical Scope definitions instead of creating an independent selector authority.

#### Scenario: A limited connector edits a protective Scope

- **WHEN** a limited connector attempts to delete or weaken a Scope referenced by the host ceiling
- **THEN** mutation admission refuses before that protection changes
- **AND** the connector's owner label cannot authorize self-widening

### Requirement: Arming occurs only during stopped and drained maintenance

Arming SHALL validate host configuration and canonical Scope references under existing maintenance authority. It SHALL durably enroll the required compatibility capability before publishing the portable armed requirement. Startup SHALL require agreement among configuration, compatibility state, and that requirement. Missing state SHALL NOT silently disarm protection.

#### Scenario: Arming crashes after compatibility enrollment

- **WHEN** the process stops after enrollment but before portable requirement publication
- **THEN** startup refuses to serve the inconsistent armed state
- **AND** repair preserves the compatibility guard instead of treating the vault as unarmed

#### Scenario: The configured path disappears after arming

- **WHEN** an armed runtime loses its configuration path, environment binding, or referenced Scope
- **THEN** it refuses content service until valid configuration is restored
- **AND** no missing-state fallback grants unrestricted access

#### Scenario: Maintenance starts while serving is active

- **WHEN** arming has not obtained stopped and drained maintenance authority
- **THEN** it performs no enrollment or publication
- **AND** it does not invent a second store-custody or authentication protocol

### Requirement: Offline import uses existing mutation and restore owners

Delivery C SHALL publish only while the destination is stopped and drained. It SHALL use existing bounded mutation, journal, and restore owners. Exact retries SHALL preserve operation identity and payload. Recovery SHALL classify durable fingerprints without repeating semantic decisions; mixed state SHALL remain unavailable until reconciled.

#### Scenario: Import acknowledgement is lost

- **WHEN** a bounded import effect committed but its acknowledgement was lost
- **THEN** an identical retry recognizes the exact committed state without repeating the effect
- **AND** changed payload or operation bindings conflict

#### Scenario: Rollback encounters later work

- **WHEN** destination content changed after the import snapshot
- **THEN** rollback accounts for each later create, edit, move, and delete before restoration
- **AND** it never silently overwrites later work or rewinds receipt evidence
