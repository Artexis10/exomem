## ADDED Requirements

### Requirement: Background speech on a one-core Linux service

Speech recognition and speaker labeling SHALL use the existing background scheduling priority. The documented one-core Linux deployment profile SHALL place the serving process and its media descendants on one shared logical CPU. The deployment SHALL assign that CPU. Work SHALL retain its existing watchdog, retry and result publication contracts.

#### Scenario: Foreground work arrives during speech processing

- **WHEN** served interactive work and an active speech job share the assigned CPU under the one-core quota
- **THEN** the kernel schedules the serving process ahead of lower-priority media work
- **AND** the media worker retains progress opportunities
- **AND** accepted media and completed output remain intact

#### Scenario: Capacity returns

- **WHEN** foreground work subsides and the speech job is still within its watchdog budget
- **THEN** the existing worker continues the same job and publishes a single current result

#### Scenario: A different deployment has no shared CPU placement

- **WHEN** the service runs outside the documented one-core Linux profile
- **THEN** the existing CPU, memory and watchdog controls still apply
