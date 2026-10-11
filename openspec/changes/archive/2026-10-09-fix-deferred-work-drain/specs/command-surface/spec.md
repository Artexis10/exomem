## ADDED Requirements

### Requirement: Persisted Configuration Failures Are Loud

A command that persists configuration SHALL NOT report or imply success unless the persisted
state changed. When the write fails, the command SHALL exit non-zero, SHALL report one
operator-readable line naming the configuration path and the remediation, and SHALL NOT
leave a temporary artifact behind.

This applies to `exomem mode`, whose configuration file is written by the service account on
a service-managed install and is therefore not always writable by the invoking user.

#### Scenario: Unwritable config fails visibly

- **WHEN** `exomem mode <target>` is run and the configuration file cannot be replaced
- **THEN** the command exits non-zero
- **AND** the output names the configuration path and how to remediate the permission
- **AND** the output is not a bare interpreter traceback

#### Scenario: A failed write leaves no orphaned temporary

- **WHEN** persisting the mode fails after a temporary file was created
- **THEN** that temporary file is removed
- **AND** a later successful run is not blocked by residue from the failed one

#### Scenario: Reported mode reflects persisted state

- **WHEN** `exomem mode` reports the mode after a set operation
- **THEN** the reported value is read back from the persisted configuration
- **AND** it is never an echo of the requested value that was not written

#### Scenario: Status reflects the effective mode

- **WHEN** a mode change did not persist
- **THEN** no surface reports the requested mode as active
