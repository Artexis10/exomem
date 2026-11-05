## ADDED Requirements

### Requirement: Quiet Mode Keeps Deterministic Upkeep Running Within Its Budgets

Quiet mode SHALL stop GPU use and model preload. It SHALL NOT stop the Dreamer's deterministic background upkeep, which loads no model. The Dreamer SHALL start a tick in quiet mode under the same gates and budgets as in any other mode, and quiet mode SHALL NOT add a separate budget. In quiet mode the Dreamer's standing per-tick and hourly budgets are the throttle that the `quiet` mode requirement asks of background maintenance ("throttled or deferred"). The sensor child, which runs on CPU in its own process, SHALL follow its own gate and SHALL NOT stop because of quiet mode.

#### Scenario: A busy vault in quiet mode still gets upkeep

- **WHEN** the effective mode is `quiet`, the Dreamer is on, requests end and begin throughout the day, and the last tick completed more than `MAX_DEFER_SECONDS` ago
- **AND** no request is live and the graph owes no work
- **THEN** the Dreamer starts a tick within its per-tick page, CPU and wall budgets
- **AND** the tick stops before its next page on the first request or vault change

#### Scenario: Quiet mode does not stop the sensor child

- **WHEN** a sensor child runs and the compute mode becomes `quiet`
- **THEN** the child keeps running, and its own gate still stops it for the setting or standby
