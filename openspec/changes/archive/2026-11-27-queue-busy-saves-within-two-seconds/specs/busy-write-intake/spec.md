## MODIFIED Requirements

### Requirement: A busy save is accepted durably instead of refused

The serving HTTP process SHALL accept a save durably when the vault mutation boundary stays busy, instead of returning `MUTATION_BUSY`. Acceptance SHALL persist the save to a machine-local store that survives a process kill and a power loss before the receipt is returned. An eligible save SHALL wait for the boundary for at most the intake wait (2 s by default) before it is accepted.

#### Scenario: A save during a long hold comes back queued

- **WHEN** a long writer holds the boundary
- **AND** the owner sends an `observe_memory` save through MCP
- **THEN** within about the intake wait the call returns `success: true`, `state: "queued"`, `committed: false`, and a `queued_write.ref` of the form `exomem://queued-write/<id>`
- **AND** `user_summary` starts with "Queued, not saved yet"
- **AND** the reply names no page title or id

#### Scenario: A save during start-up comes back queued

- **WHEN** the serving process has just started, or taken over after an upgrade, and still waits for retrieval to be ready
- **AND** the owner sends an eligible save that meets a busy boundary
- **THEN** the save is queued as during a long hold, because the drainer starts before the start-up waits

### Requirement: Queued saves land once, in arrival order

One drainer in the serving process SHALL apply queued saves in acceptance order, through the ordinary dispatcher, as the principal that sent each save. A contention refusal during application SHALL leave the save queued and retried, without a limit on attempts. A new eligible save that arrives while saves are queued SHALL be queued behind them at once, without waiting for the queue to empty.

#### Scenario: Two clients' saves land after the hold ends

- **WHEN** saves from two clients come back queued during a long hold
- **AND** the hold ends
- **THEN** every queued save commits exactly once, in the order it was accepted

#### Scenario: An edit sent after a queued create runs after it

- **WHEN** a `remember` save is queued
- **AND** an `edit_memory` of that page arrives while the create is still queued
- **THEN** the edit is applied after the create

#### Scenario: A save behind queued saves gets its receipt at once

- **WHEN** saves are queued and not yet applied
- **AND** the owner sends another eligible save
- **THEN** the call returns its queued receipt without waiting the intake wait
- **AND** the save is applied after the earlier ones
