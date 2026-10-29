## ADDED Requirements

### Requirement: A Superseded Source Owes No Compilation

The `unprocessed_source` queue SHALL NOT list a Source whose status resolves to the superseded lifecycle class. A page of that class was replaced by a live page, so no derived note is owed for it. The system SHALL decide this from the lifecycle class that the status registry declares, never from a status label.

#### Scenario: A replaced extraction is not backlog

- **WHEN** a Source page has a status of the superseded class and no note compiles it
- **THEN** the `unprocessed_source` audit does not list it

#### Scenario: A live Source still waits for compilation

- **WHEN** a Source page has a live status and no note compiles it
- **THEN** the `unprocessed_source` audit lists it as before
