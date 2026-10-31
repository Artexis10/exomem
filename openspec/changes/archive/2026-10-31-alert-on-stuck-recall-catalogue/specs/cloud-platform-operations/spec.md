## MODIFIED Requirements

### Requirement: Monitoring spans independent failure domains

The platform SHALL use its checked observability contract for external availability and backup-age checks, scheduler metrics and alerts.
At least one availability and backup-age signal SHALL run outside the production node.
Signals SHALL distinguish unavailable, stale, failed and unknown from a successful zero value.
Logs SHALL retain operational identifiers and timing without credentials, identity claims, vault content or response bodies.
Alerts SHALL cover unavailable cells, recall that serves a stalled catalogue, overdue work, backup age, restore failure, disk and node pressure, scheduler failure, secret-version mismatch and attachment exhaustion.
Thresholds SHALL come from the checked observability and schedule contracts rather than obsolete Hosted limits.
A Cloud cell SHALL serve its readiness answer on a health-only port that answers no other request.
cellctl SHALL read cell readiness only on that port, and network policy and admission SHALL admit cellctl to no other cell port.
The stalled-recall alert SHALL fire while a serving cell reports its recall lagging on a catalogue that the cell has measured unchanged for at least the contract threshold.
Each serving cell SHALL keep its last definite verdict, lagging past the threshold or strictly ready; an unread cell, a lag with no age and a younger lag SHALL leave that verdict unchanged.
The alert SHALL resolve only when the last verdict of every serving cell is strictly ready, and SHALL send no resolution while a serving cell has had no verdict since cellctl started.

#### Scenario: The production node disappears
- **WHEN** the node and in-cluster monitors become unavailable
- **THEN** an external signal detects unavailable service and stale backup evidence

#### Scenario: A ready cell's recall catalogue stops advancing
- **WHEN** a serving cell stays ready while it reports recall lagging on a catalogue unchanged for the contract threshold
- **THEN** cellctl sends the stalled-recall alert through the alert receiver and logs only the cell's id

#### Scenario: A cell restarts during a stall
- **WHEN** the restarted cell reports a younger lag, or does not answer
- **THEN** cellctl sends no resolution until the cell reads its recall strictly ready

#### Scenario: One cell catches up while another says nothing new
- **WHEN** a lagging cell reads its recall strictly ready, and another serving cell that last read strictly ready lags briefly or does not answer
- **THEN** cellctl resolves the stalled-recall alert

#### Scenario: cellctl reaches for the cell port
- **WHEN** cellctl connects to a cell's MCP port, or writes cell ingress that would admit it there
- **THEN** network policy refuses the connection and admission refuses the write

#### Scenario: The health-only port receives another request
- **WHEN** a peer sends the health-only port any request other than `GET /health/ready`
- **THEN** the cell answers 404 with an empty body
