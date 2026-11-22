## ADDED Requirements

### Requirement: A busy save is accepted durably instead of refused

The serving HTTP process SHALL accept a save durably when the vault mutation boundary stays busy, instead of returning `MUTATION_BUSY`. A save SHALL be accepted only when the intake is enabled, a drainer runs in the same process, the command declares `busy_intake` in the command registry, the refusal is `MUTATION_BUSY`, `MUTATION_WARMING` or `COLLECTION_STORE_BUSY` with `committed: false`, the principal is the vault owner without a delegated authorization session, and the serialised arguments fit within 4 MiB. Acceptance SHALL persist the save to a machine-local store that survives a process kill and a power loss before the receipt is returned. An eligible save SHALL wait for the boundary for at most the intake wait (15 s by default) before it is accepted. Any save that is not accepted SHALL keep the existing `MUTATION_BUSY` contract.

#### Scenario: A save during a long hold comes back queued

- **WHEN** a long writer holds the boundary
- **AND** the owner sends an `observe_memory` save through MCP
- **THEN** within about the intake wait the call returns `success: true`, `state: "queued"`, `committed: false`, and a `queued_write.ref` of the form `exomem://queued-write/<id>`
- **AND** `user_summary` starts with "Queued, not saved yet"
- **AND** the reply names no page title or id

#### Scenario: An ineligible write keeps the busy refusal

- **WHEN** a `maintain_memory` write meets the same busy boundary
- **THEN** it returns `MUTATION_BUSY` with `committed: false` as before

#### Scenario: A delegated caller keeps the busy refusal

- **WHEN** a caller with a delegated authorization session sends an eligible save while the boundary is busy
- **THEN** it returns `MUTATION_BUSY` with `committed: false` as before

#### Scenario: A process without a drainer keeps the busy refusal

- **WHEN** a stdio server or the CLI meets a busy boundary on an eligible save
- **THEN** it returns `MUTATION_BUSY` and queues nothing

### Requirement: Queued saves land once, in arrival order

One drainer in the serving process SHALL apply queued saves in acceptance order, through the ordinary dispatcher, as the principal that sent each save. A contention refusal during application SHALL leave the save queued and retried, without a limit on attempts. The drainer SHALL replay each save under its own durable replay identity, so a save whose commit happened before a process stop is not written again after the restart. A replay that stopped before it held the vault boundary SHALL be replayed under a fresh identity, so a dead attempt never leaves a save stuck. A replay that held the boundary and whose commit cannot be proven or ruled out SHALL end `failed` with `MUTATION_OUTCOME_UNKNOWN`, keep its arguments, and SHALL NOT block the saves behind it. While a save is queued or being applied, a resend with the save's original idempotency identity SHALL return the same receipt and SHALL NOT run. A new eligible save that arrives while saves are queued SHALL wait behind them and SHALL be queued behind them if the queue does not empty within the intake wait.

#### Scenario: Two clients' saves land after the hold ends

- **WHEN** saves from two clients come back queued during a long hold
- **AND** the hold ends
- **THEN** every queued save commits exactly once, in the order it was accepted

#### Scenario: A restart mid-queue loses nothing and duplicates nothing

- **WHEN** the service stops while saves are queued and one is waiting for the boundary to apply
- **AND** the service starts again
- **THEN** every queued save commits exactly once

#### Scenario: A stop inside an uncertain commit is visible, not stuck

- **WHEN** the service stops after a replay held the boundary and before its commit could be proven
- **AND** the service starts again without commit evidence for it
- **THEN** that save is recorded `failed` with `MUTATION_OUTCOME_UNKNOWN` and its arguments
- **AND** the saves behind it are applied

#### Scenario: A resend while queued runs nothing

- **WHEN** a save is queued
- **AND** the caller resends the same call with the same idempotency identity
- **THEN** the reply is the same queued receipt
- **AND** only one commit results

#### Scenario: An edit sent after a queued create runs after it

- **WHEN** a `remember` save is queued
- **AND** an `edit_memory` of that page arrives while the create is still queued
- **THEN** the edit is applied after the create

### Requirement: A failed queued save stays visible

A queued save whose application ends in a refusal other than contention SHALL be recorded as `failed` with the public refusal and SHALL keep its arguments. The service SHALL NOT change a queued save to make it apply, and SHALL NOT delete a failed save by itself. The owner SHALL be able to list queued and failed saves and dismiss a failed one with `exomem intake list` and `exomem intake dismiss <id>`. An applied save SHALL keep its compact outcome and drop its arguments.

#### Scenario: A stale edit fails visibly

- **WHEN** a queued `edit_memory` carries a hash guard
- **AND** the page changes before the edit is applied
- **THEN** the save is recorded `failed` with the guard's refusal
- **AND** reading it returns the refusal and the original arguments

### Requirement: Callers can read a queued save's outcome

`review_memory(mode="queued-write")` SHALL list the queued, applying and failed saves, oldest first and bounded by `limit`. With `ref` it SHALL return that save's state (`queued`, `applying`, `applied` or `failed`), its attempts, its compact outcome when applied, its refusal when failed, and its arguments while queued or failed. `coordination_status` SHALL report the queued, applying and failed counts and the age of the oldest queued save, and SHALL report `unknown` when the store cannot be read.

#### Scenario: A later session checks a queued save

- **WHEN** a save was queued in one session
- **AND** a later session reads its `ref` after the save landed
- **THEN** the read reports `applied` with the compact outcome of the commit

#### Scenario: An unreadable store is not reported as empty

- **WHEN** the intake store cannot be read
- **THEN** `coordination_status` reports the intake as `unknown`, not as zero saves
