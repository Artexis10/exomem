## MODIFIED Requirements

### Requirement: Auto-Quiet Is Optional And Soft-Fail

The system SHALL keep automatic quiet-mode switching disabled by default. When
enabled, auto-quiet SHALL observe local machine pressure signals such as GPU
pressure or memory pressure without importing torch or allocating CUDA solely for
the probe. If probes are unavailable, fail, or are unsupported on the platform,
auto-quiet SHALL log or report the unavailable signal and continue with manual
mode behavior. Auto-quiet MUST NOT override an explicit `EXOMEM_MODE` environment
pin.

A pressure signal SHALL be attributable: load that Exomem itself generates SHALL
NOT be reported as pressure. Restoring a previously recorded mode SHALL NOT
re-arm the condition that caused the demotion. A pressure threshold SHALL be
bounded against what Exomem actually needs to run rather than against total
device capacity, and SHALL never be unsatisfiable on a supported device.

#### Scenario: Auto-quiet is disabled by default

- **WHEN** no auto-quiet configuration is enabled
- **THEN** Exomem changes modes only through the existing manual mode controls

#### Scenario: Pressure enters and leaves quiet

- **WHEN** auto-quiet is enabled
- **AND** the configured pressure signal remains active past the hysteresis window
- **THEN** Exomem switches the config-file mode to `quiet` and records the prior
  config-file mode
- **AND** when pressure clears past the restore window, Exomem restores the prior
  mode unless the user changed modes manually while pressure was active

#### Scenario: Probe failure does not change mode

- **WHEN** auto-quiet is enabled but every configured pressure probe is unavailable
  or fails
- **THEN** Exomem does not crash
- **AND** Exomem does not change the current mode based on the failed probe

#### Scenario: Exomem's own GPU work is not co-tenant pressure

- **WHEN** auto-quiet is enabled and Exomem runs in a mode that places its own
  models on the GPU
- **AND** the device reports high utilization attributable to this process
- **THEN** auto-quiet does not enter quiet mode
- **AND** the mode the user selected remains in effect

#### Scenario: A restored mode does not re-arm its own trigger

- **WHEN** auto-quiet has entered quiet mode and the pressure signal clears
- **AND** the restored mode resumes Exomem's own GPU work
- **THEN** that work alone does not drive a further demotion
- **AND** the configured mode remains stable rather than oscillating

#### Scenario: The pressure signal cannot be read

- **WHEN** a GPU pressure probe runs but reports a field it cannot parse
- **THEN** the status reports the signal as unavailable rather than as absence of
  pressure
- **AND** the reported posture is distinguishable from a genuinely idle device

#### Scenario: A pressure threshold stays satisfiable on a small device

- **WHEN** the configured pressure threshold is resolved for a device whose total
  capacity is at or below Exomem's own placement requirement
- **THEN** the threshold does not exceed what the device can report as free
- **AND** auto-quiet does not latch into quiet mode on an otherwise idle device
