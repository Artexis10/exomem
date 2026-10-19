## MODIFIED Requirements

### Requirement: The f23 family runs against the real runtime

An installed journey SHALL run seed writes, maintenance, a dismissal, an engine restart, prominence changes across the level range, and a bulk ingest. It SHALL project the resulting review state and emission ledger into the snapshot pair. The vault projector SHALL declare `due_state_counters` available through the projection file. Without an installed envelope, the driver SHALL refuse to run.

#### Scenario: f23 reports what this runtime can decide, and no more

- **WHEN** the f23 journey runs against the current runtime
- **THEN** `dismissal_respected_across_passes` passes for the dismissed subject
- **AND** `counter_emission_not_repeated_per_write` uses only the emission delta between the two snapshots
- **AND** a batch with zero delivered blocks reports `unsupported`, never a vacuous pass or an earlier batch's delivery
- **AND** a source-only ingest has zero emissions because its affected pages have no due items
- **AND** separate compiled-page carrier checks prove that a batch delivers at most one eligible block

#### Scenario: Removing the batch scope turns the counter assertion red

- **WHEN** the batch scope is disabled and twelve write carriers run over one vault
- **THEN** `counter_emission_not_repeated_per_write` fails with twelve emissions for twelve writes
