## ADDED Requirements

### Requirement: The gateway holds a request while its cell restarts

When the gateway cannot connect to a running cell and no byte reached it, it SHALL retry the same cell at a configured interval with jitter, until a configured budget runs out. It SHALL then answer `503 CELL_NOT_READY` with `Retry-After`. It SHALL hold only a request whose body fits the gateway's existing prepared-body cap, and SHALL buffer it through that same reader. It SHALL never retry after a connection opened. Held requests SHALL count against their own configured caps, not the in-flight caps, until the cell accepts the connection. A held request that the cell accepts SHALL then need a free in-flight slot. A budget of 0 SHALL turn the hold off.

#### Scenario: The cell returns within the budget

- **WHEN** a cell refuses connections during its upgrade, and starts to accept them before the budget runs out
- **THEN** the gateway forwards the held request, and the client receives the cell's answer with no error

#### Scenario: The budget runs out

- **WHEN** a cell refuses connections for longer than the budget
- **THEN** the gateway answers `503 CELL_NOT_READY` with a `Retry-After` header

#### Scenario: The connection breaks after bytes reached the cell

- **WHEN** the connection to the cell closes after the gateway sent part of the request
- **THEN** the gateway does not retry the request and answers with an error

#### Scenario: The body is over the cap

- **WHEN** a request's body is larger than the hold's body cap, and the cell refuses the connection
- **THEN** the gateway answers `503 CELL_NOT_READY` at once, without a hold

#### Scenario: The hold is full

- **WHEN** the held requests of a tenant reach the per-tenant hold cap
- **THEN** the tenant's next held request gets `503` with `Retry-After` at once
- **AND** another tenant's requests keep their in-flight slots

#### Scenario: The cell accepts while the in-flight slots are full

- **WHEN** a held request's connection is accepted, and its tenant and client have no free in-flight slot
- **THEN** the gateway answers as a full in-flight cap answers, and the hold slot is free again

#### Scenario: The client leaves

- **WHEN** the client disconnects during a hold
- **THEN** the gateway stops retrying at once

#### Scenario: The hold is off

- **WHEN** the configured budget is 0, and the cell refuses the connection
- **THEN** the gateway answers `503 CELL_NOT_READY` at once

#### Scenario: The hold stays with the tenant's cell

- **WHEN** a request waits in the hold
- **THEN** each retry goes to the same cell, and no other tenant's cell is tried

### Requirement: Every gateway call to a cell has a timeout

Each connect attempt to a cell SHALL end after a configured connect timeout. When a cell has sent no response headers within a configured upstream timeout, the gateway SHALL answer `504 CELL_TIMEOUT` and free the request's slot. When a streamed response sends no bytes for a configured idle time, the gateway SHALL close the stream and free the request's slot.

#### Scenario: A cell hangs

- **WHEN** a cell accepts a request and sends no response headers within the upstream timeout
- **THEN** the gateway answers `504 CELL_TIMEOUT`, and the request's in-flight slot is free again

#### Scenario: A cell hangs mid-stream

- **WHEN** a cell sends response headers, and then sends no body bytes for the configured idle time
- **THEN** the gateway closes the stream, and the request's in-flight slot is free again

#### Scenario: A slow call stays within the timeout

- **WHEN** a cell sends its response headers before the upstream timeout
- **THEN** the gateway streams the response to the client
