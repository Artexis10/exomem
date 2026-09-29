## ADDED Requirements

### Requirement: Both listeners pause and drain together

When the local listener is open, it SHALL forward through the same ingress as the public
listener, so an upgrade SHALL pause, queue, drain and resume both together, and each
worker the manager spawns, standby included, SHALL receive the same ingress proof. The
manager SHALL stop the local listener whenever it stops the public one.

#### Scenario: A local call arrives during replacement
- **WHEN** a request reaches the local listener while a worker is being replaced
- **THEN** it is held under the same admission bounds as a public request and forwarded once to the ready replacement with the local stamp

#### Scenario: A finite local request is in flight when an upgrade begins
- **WHEN** a local request has been forwarded and has not completed
- **THEN** the drain waits for it as it would for a public request
