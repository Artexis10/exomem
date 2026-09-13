## Purpose

Keep a managed Linux/WSL service available to existing clients while replacing its single state-owning worker, with bounded requests and recoverable operator control.

## ADDED Requirements

### Requirement: Managed upgrades preserve the public endpoint
The system SHALL keep the public HTTP listener and existing accepted connections alive throughout a managed worker upgrade. Release download and installation SHALL finish before the existing worker stops accepting requests.

#### Scenario: Calls arrive during replacement
- **WHEN** calls arrive while a managed worker is being replaced within the configured handoff budget
- **THEN** the public endpoint accepts and holds them until the verified replacement is ready
- **AND** each admitted request is forwarded once

#### Scenario: Preparation fails
- **WHEN** release staging or target verification fails before handoff
- **THEN** the existing worker continues serving without a restart

### Requirement: Upgrades retain operation ownership
The system SHALL let forwarded finite requests finish before stopping their worker. It SHALL NOT automatically replay a request that was forwarded or treat a client disconnect as proof that an operation stopped.

#### Scenario: Mutation is active
- **WHEN** a mutation is executing as an upgrade begins
- **THEN** the upgrade waits for its complete response before stopping the worker
- **AND** the mutation executes once

#### Scenario: Client disconnects after dispatch
- **WHEN** a client disconnects after its operation reached the worker
- **THEN** that operation remains part of the drain until the upstream response completes

#### Scenario: Drain deadline expires
- **WHEN** finite requests do not finish within the drain budget
- **THEN** the upgrade aborts before signalling the worker and reopens admission to that worker

### Requirement: Handoff admission is bounded and explicit
The system SHALL bound queued request count, memory, body intake time and wait time. A refused request SHALL remain undispatched and receive a protocol-compatible error that identifies this fact when its request identity is available.

#### Scenario: Queue expires
- **WHEN** a queued MCP request exceeds the wait budget
- **THEN** it receives an error saying it was not dispatched
- **AND** the same connected client can make a subsequent call after service recovery

#### Scenario: Admission capacity is exhausted
- **WHEN** a new request exceeds the count or byte bounds
- **THEN** it is refused without executing a tool or allocating unbounded memory

### Requirement: Only one worker owns state during an upgrade
The system SHALL prove the previous worker and its owned descendants have exited before running offline migration or starting its replacement. The upgrade SHALL preserve the configured vault, external state root, issuer and OAuth authority.

#### Scenario: Old child survives shutdown
- **WHEN** an owned child remains alive after the worker exits
- **THEN** offline migration and replacement startup remain blocked until that child is stopped and its exit proven

#### Scenario: Replacement fails
- **WHEN** migration or replacement readiness fails after the old worker stopped
- **THEN** a durable recovery record identifies the incomplete transition
- **AND** neither an old release nor a second worker is silently started

### Requirement: Upgrade control is private and recoverable
The system SHALL expose upgrade control only through an owner-restricted local channel, serialize transitions and retain their outcome independently of the control client's connection.

#### Scenario: Remote control attempt
- **WHEN** a public HTTP client attempts to invoke an upgrade command
- **THEN** no upgrade control route or executable-selection surface is available

#### Scenario: Operator disconnects
- **WHEN** the operator connection closes after an upgrade is accepted
- **THEN** the supervisor completes or records the failed transition and reports its state to a later status request

#### Scenario: Supervisor restarts mid-transition
- **WHEN** the supervisor starts with an incomplete upgrade record
- **THEN** it does not launch the prior release and offers explicit roll-forward recovery
