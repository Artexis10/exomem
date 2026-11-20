## ADDED Requirements

### Requirement: An abandoned passage request stops and frees its admission

When the caller of a passage request leaves before the reply, the cell SHALL stop that request at its next phase boundary and release its admission capacity.
The phase boundaries SHALL be the points before eligibility, query encoding, chunk selection and byte verification.
A stopped request SHALL NOT start a later phase.
The cell SHALL log each stopped request with the phase it stopped before.
A request without a serving host, such as a command-line request, SHALL run to completion.

#### Scenario: The caller leaves during eligibility

- **WHEN** a caller abandons a passage request while the cell resolves the request's eligible Sources
- **THEN** the request stops before it encodes the query or reads a Source
- **AND** its admission capacity is free for the next request

#### Scenario: A request has no serving host

- **WHEN** a passage request runs from the command line
- **THEN** the request runs every phase to completion
