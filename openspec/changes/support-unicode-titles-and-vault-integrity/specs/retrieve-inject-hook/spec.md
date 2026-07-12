## ADDED Requirements

### Requirement: Multi-Client Hook Checks Skip Absent Clients

The default hook health check SHALL distinguish a client with no installation or configuration footprint from a partially configured or broken client. An absent client SHALL be reported as skipped and MUST NOT make the aggregate result fail. An explicitly requested client SHALL still be checked strictly.

#### Scenario: One supported client is not installed

- **WHEN** the default check examines two supported clients, one is healthy, and the other has no config, hook directory, scripts, logs, or cache
- **THEN** the absent client is reported as skipped
- **AND** the aggregate result succeeds

#### Scenario: Partial client installation still fails

- **WHEN** a client has a config or hook footprint but required current hook wiring or scripts are missing
- **THEN** that client is checked and reported failed
- **AND** the aggregate result fails

#### Scenario: Explicit absent client is strict

- **WHEN** the caller explicitly selects a client that has no installation footprint
- **THEN** the check reports that selected client as not installed and exits non-zero
