## MODIFIED Requirements

### Requirement: Safe normal-mode residency
Normal mode SHALL avoid startup model and O(vault) cache preloads and SHALL run idle resource
reclamation by default. In normal mode an idle CPU cache SHALL be released only when the host's
available memory or the service cgroup's headroom falls below the configured floor, and SHALL be
released on idle when neither reading is available. Performance mode MAY preload when explicitly selected. Environment
overrides SHALL remain available for deliberate operator choices.

#### Scenario: Normal-mode startup
- **WHEN** the service starts in normal mode
- **THEN** models and O(vault) CPU caches remain lazy
- **AND** the idle reaper is active

#### Scenario: Explicit performance mode
- **WHEN** an operator selects performance mode
- **THEN** the service may preload latency-oriented resources
- **AND** the choice is visible through resource status

#### Scenario: Normal mode keeps an idle recall cache while memory is plentiful
- **WHEN** the effective mode is `normal`
- **AND** a find-related RAM cache has been idle longer than the idle threshold
- **AND** available memory and cgroup headroom are both above the configured floor
- **THEN** the idle reaper keeps that cache resident

#### Scenario: Normal mode releases an idle recall cache under memory pressure
- **WHEN** the effective mode is `normal`
- **AND** a find-related RAM cache has been idle longer than the idle threshold
- **AND** available memory or cgroup headroom is below the configured floor, or neither can be read
- **THEN** the idle reaper releases that cache
