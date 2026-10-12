## MODIFIED Requirements

### Requirement: Bootstrap Profiles

The system SHALL support `compact`, `full`, and `diagnostics` bootstrap profiles.
`compact` SHALL be the default. `full` SHALL include concrete workflow examples.
`diagnostics` SHALL include performance interpretation guidance for timing and
compute-mode discussions.

Compact sections and skill-aware session bootstrap SHALL serve the same latency
advisory from the calling client's retained in-process ring. Each row SHALL contain
exactly `tool`, `deep`, `samples`, `p50_ms`, `p90_ms`, `ceiling_ms`, `bar_id`,
`contract_revision`, and `source`. The bar identity and contract revision SHALL
resolve the measurement profile, population, reference, window, minimum samples and outcome
inclusion. `samples` SHALL count eligible retained observations with known timing;
unknown timing MUST NOT become zero. The advisory MUST NOT claim coverage or
installed acceptance.

Full and diagnostics profiles SHALL retain every summary field except the caller
identity, including dominant spans and coverage. Every profile SHALL omit latency
when the calling client has no breach. The compact core SHALL retain its existing
breach pointer. The independent doctor report SHALL retain its own source coverage;
it MUST NOT be presented as an exact expansion of the bootstrap advisory.

#### Scenario: Diagnostics profile includes performance guidance

- **WHEN** `bootstrap(profile="diagnostics")` is called
- **THEN** the response includes guidance for normal lookup, reasoning lookup, and diagnostics lookup
- **AND** the guidance distinguishes compute mode from retrieval knobs such as `rerank`, `pack`, and `include_timings`

#### Scenario: Compact and session preserve the same breach identity

- **WHEN** a calling client's retained observations breach a watch bar
- **THEN** compact sections and session bootstrap serve the same nine-field advisory
- **AND** its bar identity resolves the measurement scope without a coverage or acceptance claim

#### Scenario: Full profiles retain the attribution detail

- **WHEN** the calling client requests full or diagnostics bootstrap during a breach
- **THEN** the response retains every dominant span and the retained-ring coverage
- **AND** it does not expose another client's observations or raw caller identity

#### Scenario: Populated compact sections keep their existing ceiling

- **WHEN** every maintained watch pair breaches with all dominant spans present
- **THEN** the compact diagnostics section remains within its existing byte ceiling on every maintained surface
- **AND** the full profiles retain those spans without changing the latency bars
