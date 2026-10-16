## ADDED Requirements

### Requirement: Only the managed migrator holds the operator credential
When the target release declares the collections migration, the vault holds the standby's pre-import, and the protected operator file beside the service environment file is a regular file of the service user that no other user can read or write, the supervisor SHALL read the coordinator's operator credential from that file and pass it to the migrator child's environment alone. The serving worker and the standby SHALL never receive it, and no command line or record SHALL carry it. A missing, unreadable or unprotected file SHALL skip the collections step with its reason in the handoff record and SHALL NOT fail the upgrade. A target that declares no collections migration SHALL be upgraded exactly as before.

#### Scenario: Only the migrator child receives the credential
- **WHEN** a managed upgrade runs a collections migration
- **THEN** the migrator child's environment holds the operator credential
- **AND** the serving worker's and the standby's environments and every command line do not

#### Scenario: Without a usable operator file the upgrade completes in file mode
- **WHEN** the operator file is missing or another user can read it
- **THEN** the upgrade completes, the vault stays in file mode, and the handoff record names the reason

#### Scenario: A release that keeps the migration dark upgrades as before
- **WHEN** the target release declares no collections migration
- **THEN** the handoff record and the migrator's command are what they were before the migration existed
