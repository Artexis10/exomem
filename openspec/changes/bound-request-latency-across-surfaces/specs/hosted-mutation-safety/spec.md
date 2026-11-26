## ADDED Requirements

### Requirement: A Write Refused For Its Spent Call Time Names That Cause

A write refused because its call's time ran out before its commit SHALL carry `cause: request_budget`. No wait for the boundary SHALL absorb this refusal, on the in-line or the store route. A queued save SHALL record this cause as its wait reason.

#### Scenario: A store-routed write keeps the cause

- **WHEN** a keyed write routed through the store thread reaches its first write guard after its call's deadline
- **THEN** the caller receives `MUTATION_BUSY` with `cause: request_budget`, not `capture_wait_exhausted`

#### Scenario: A queued save records why it waited

- **WHEN** the write intake queues a save refused for its spent call time
- **THEN** the mutation journal row for the queued save carries `request_budget` as its code
