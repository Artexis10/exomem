## ADDED Requirements

### Requirement: Resource Mode CLI Is Scriptable

The system SHALL expose resource mode controls through the CLI without requiring
code edits. The CLI SHALL support showing the current mode and setting the mode
to `quiet`, `normal`, or `performance`. Low-resource aliases such as
`resource-saver` or `low-resource`, if accepted, MUST normalize to `quiet`.
Machine-readable output SHALL include the effective mode, source, config path,
and resolved resource policy fields.

#### Scenario: Show resource mode as JSON

- **WHEN** the user runs the mode command with JSON output enabled
- **THEN** the command emits stable JSON containing the effective mode, mode
  source, config path, and resource policy fields
- **AND** the command exits with status 0

#### Scenario: Low-resource alias maps to quiet

- **WHEN** the user sets the mode through an accepted low-resource alias
- **THEN** the persisted canonical mode is `quiet`
- **AND** subsequent mode status reports `quiet`

#### Scenario: Running server applies CLI mode change

- **WHEN** the CLI writes a new config-file mode
- **THEN** a running server observes the change through the existing mode-watch
  mechanism and applies the corresponding resource policy without a restart

### Requirement: Resource Status CLI Is No-Allocation

The system SHALL expose a scriptable resource status command or mode-status flag
that reports residency and deferred-work diagnostics without allocating heavy
resources. It MUST NOT load models, create sidecars, read vector matrices, or
initialize CUDA solely to answer status.

#### Scenario: Status is safe before gaming

- **WHEN** the user runs the resource status command before starting a foreground
  workload
- **THEN** the command reports mode policy, loaded models, large-cache residency,
  deferred work, and CUDA accounting when already initialized
- **AND** the command does not initialize CUDA or load any absent model/cache

#### Scenario: Unknown probes are represented explicitly

- **WHEN** a platform-specific resource metric cannot be read without allocation
  or without an unavailable dependency
- **THEN** the JSON status reports that metric as unknown or unavailable
- **AND** the command still exits successfully if the rest of status collection
  succeeds
