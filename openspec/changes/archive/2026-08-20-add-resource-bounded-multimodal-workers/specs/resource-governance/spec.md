## ADDED Requirements

### Requirement: Safe normal-mode residency
Normal mode SHALL avoid startup model and O(vault) cache preloads and SHALL run idle resource
reclamation by default. Performance mode MAY preload when explicitly selected. Environment
overrides SHALL remain available for deliberate operator choices.

#### Scenario: Normal-mode startup
- **WHEN** the service starts in normal mode
- **THEN** models and O(vault) CPU caches remain lazy
- **AND** the idle reaper is active

#### Scenario: Explicit performance mode
- **WHEN** an operator selects performance mode
- **THEN** the service may preload latency-oriented resources
- **AND** the choice is visible through resource status

### Requirement: Persistent-core resource acceptance envelope
Release verification SHALL measure the persistent service separately from transient workers.
After worker idle exit on the maintained fixture, the acceptance targets SHALL be no active
media worker, less than 200 MiB GPU delta/no CUDA compute process, no more than 512 MiB
persistent-core RSS before user-triggered cache growth, and less than 1% idle CPU averaged over
60 seconds.

#### Scenario: Resource verification after media work
- **WHEN** a representative media job completes and the worker idle interval elapses
- **THEN** verification records zero media workers and the persistent-core RSS/CPU/GPU metrics
- **AND** an exceeded target is treated as a release failure or explicitly documented blocker
