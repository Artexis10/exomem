## ADDED Requirements

### Requirement: A busy save is accepted durably instead of refused

The serving HTTP process SHALL accept a save durably when the vault mutation boundary stays busy, instead of returning `MUTATION_BUSY`. Acceptance SHALL persist the save to a machine-local store that survives a process kill and a power loss before the receipt is returned. An eligible save SHALL wait for the boundary for at most the intake wait (15 s by default) before it is accepted.

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

### Requirement: Only eligible saves are accepted

A save SHALL be accepted only when the intake is enabled, a drainer runs in the same process, the command registry marks the command and the operation it selects as queueable, the refusal is `MUTATION_BUSY`, `MUTATION_WARMING` or `COLLECTION_STORE_BUSY` with `committed: false`, the principal is the vault owner without a delegated authorization session, the call is keyed, and its arguments fit within 4 MiB. Any save that is not accepted SHALL keep the existing `MUTATION_BUSY` contract.

#### Scenario: An ineligible write keeps the busy refusal

- **WHEN** a `maintain_memory` write meets the same busy boundary
- **THEN** it returns `MUTATION_BUSY` with `committed: false` as before

#### Scenario: A protocol step keeps the busy refusal

- **WHEN** the owner sends an `episode_memory` `prepare` step, or a `capture_source` call that carries a capture reservation, while the boundary is busy
- **THEN** it returns `MUTATION_BUSY` with `committed: false` as before
- **AND** an `episode_memory` `record` sent in the same conditions is queued

#### Scenario: A delegated caller keeps the busy refusal

- **WHEN** a caller with a delegated authorization session sends an eligible save while the boundary is busy
- **THEN** it returns `MUTATION_BUSY` with `committed: false` as before

#### Scenario: A save without an idempotency identity keeps the busy refusal

- **WHEN** a REST save without an `Idempotency-Key` header meets a busy boundary
- **THEN** it returns `MUTATION_BUSY` and queues nothing

#### Scenario: A process without a drainer keeps the busy refusal

- **WHEN** a stdio server or the CLI meets a busy boundary on an eligible save
- **THEN** it returns `MUTATION_BUSY` and queues nothing

### Requirement: The intake stays off the uncontended path

A save that meets no contention SHALL NOT read the intake store, and a fault in the intake SHALL NOT refuse a save that would otherwise run. A drainer with nothing queued SHALL NOT write to the store or change the vault's private identities, and SHALL read it at most once every few minutes.

#### Scenario: An unreadable intake store does not block an ordinary save

- **WHEN** the intake store cannot be read
- **AND** the owner sends an eligible save while the boundary is free
- **THEN** the save commits as it would without the intake

### Requirement: Queued saves land once, in arrival order

One drainer in the serving process SHALL apply queued saves in acceptance order, through the ordinary dispatcher, as the principal that sent each save. A contention refusal during application SHALL leave the save queued and retried, without a limit on attempts. A new eligible save that arrives while saves are queued SHALL wait behind them and SHALL be queued behind them if the queue does not empty within the intake wait.

#### Scenario: Two clients' saves land after the hold ends

- **WHEN** saves from two clients come back queued during a long hold
- **AND** the hold ends
- **THEN** every queued save commits exactly once, in the order it was accepted

#### Scenario: An edit sent after a queued create runs after it

- **WHEN** a `remember` save is queued
- **AND** an `edit_memory` of that page arrives while the create is still queued
- **THEN** the edit is applied after the create

### Requirement: A replay commits at most once

The drainer SHALL replay each save under its own durable replay identity, so a commit made before a process stop is not written again. Before a replay writes its first canonical byte, the service SHALL record durably that the replay reached its commit; when that record cannot be written, the replay SHALL be refused with nothing committed and retried. A replay that stopped before it reached its commit SHALL be replayed under a fresh identity, so a dead attempt never leaves a save stuck.

#### Scenario: A restart mid-queue loses nothing and duplicates nothing

- **WHEN** the service stops while saves are queued and one is waiting for the boundary to apply
- **AND** the service starts again
- **THEN** every queued save commits exactly once

#### Scenario: A commit record that cannot be written commits nothing

- **WHEN** a replay reaches its commit and its commit record cannot be written
- **THEN** nothing is committed by that attempt
- **AND** the save is applied exactly once later

### Requirement: A stop never waits out a replay

Stopping the drainer SHALL end a replay's waits for a busy boundary, a warm-up or its own preparation. A refusal that proves nothing committed after the stop SHALL leave the save queued. While a replay still runs at the end of the stop, the service SHALL keep its writer lease.

#### Scenario: A stop ends a waiting replay promptly

- **WHEN** the service stops while a replay waits for a busy boundary, a warm-up or its own preparation
- **THEN** the replay stops at its next check, within one boundary attempt, and the save stays queued
- **AND** the next process applies the save exactly once

### Requirement: An uncertain or unauthorised replay does not block the queue

A replay that reached its commit and whose commit cannot be proven or ruled out SHALL end `failed` with `MUTATION_OUTCOME_UNKNOWN`, keep its arguments, and SHALL NOT block the saves behind it. Before each replay of a save sent by a signed-in login, the drainer SHALL recheck that login: a login that has ended SHALL end the save `failed` with `INTAKE_LOGIN_REVOKED` and its arguments, and a session store that cannot be read SHALL leave the save queued.

#### Scenario: A stop inside an uncertain commit is visible, not stuck

- **WHEN** the service stops after a replay held the boundary and before its commit could be proven
- **AND** the service starts again without commit evidence for it
- **THEN** that save is recorded `failed` with `MUTATION_OUTCOME_UNKNOWN` and its arguments
- **AND** the saves behind it are applied

#### Scenario: A save whose login has ended is not applied

- **WHEN** a save is queued
- **AND** the login that sent it ends before the save is applied
- **THEN** the save is recorded `failed` with `INTAKE_LOGIN_REVOKED` and its arguments

### Requirement: A resend of a queued save runs nothing

While a save is queued or being applied, a resend with the save's original idempotency identity and the same payload SHALL return the same receipt and SHALL NOT run; the same identity with a different payload SHALL be a different save.

#### Scenario: A resend while queued runs nothing

- **WHEN** a save is queued
- **AND** the caller resends the same call with the same idempotency identity
- **THEN** the reply is the same queued receipt
- **AND** only one commit results

#### Scenario: A reused identity with a different payload is a different save

- **WHEN** a save is queued
- **AND** the caller sends a different payload with the same idempotency key
- **THEN** both saves are applied as separate commits

### Requirement: A failed queued save stays visible

A queued save whose application ends in a refusal other than contention SHALL be recorded as `failed` with the public refusal and SHALL keep its arguments. The service SHALL NOT change a queued save to make it apply, and SHALL NOT delete a failed save by itself. The owner SHALL be able to list queued and failed saves and dismiss a failed one with `exomem intake list` and `exomem intake dismiss <id>`. An applied save SHALL keep its compact outcome and drop its arguments.

#### Scenario: A stale edit fails visibly

- **WHEN** a queued `edit_memory` carries a hash guard
- **AND** the page changes before the edit is applied
- **THEN** the save is recorded `failed` with the guard's refusal
- **AND** reading it returns the refusal and the original arguments

### Requirement: The next save names failed queued saves

When queued saves have failed and no reply has named them, the next successful reply to an owner's eligible save SHALL keep its own outcome first, then add one sentence with how many failed and the queued-write route as the serving surface spells it, plus a `queued_writes_failed` count. Each failure SHALL be named once, across restarts. A replay, a read, or any other caller's reply SHALL carry no notice and SHALL NOT use one up.

#### Scenario: A failure is named on the next save, once

- **WHEN** a queued edit fails because its page changed
- **AND** the owner then saves a note
- **THEN** the reply starts with the save's own outcome and adds "1 queued save sent earlier failed and was not saved"
- **AND** the owner's following save carries no notice
- **AND** a save queued behind the failed edit, a read, or a restart does not take or repeat the notice

### Requirement: Callers can read a queued save's outcome

`review_memory(mode="queued-write")` SHALL list open and failed saves, oldest first and bounded by `limit`. With `ref` it SHALL return that save's state (`queued`, `applying`, `applied` or `failed`), its attempts, its compact outcome when applied, its refusal when failed, and its arguments while queued or failed. `coordination_status` SHALL report the queued, applying and failed counts and the age of the oldest queued save, and SHALL report `unknown` when the store cannot be read.

#### Scenario: A later session checks a queued save

- **WHEN** a save was queued in one session
- **AND** a later session reads its `ref` after the save landed
- **THEN** the read reports `applied` with the compact outcome of the commit

#### Scenario: An unreadable store is not reported as empty

- **WHEN** the intake store cannot be read
- **THEN** `coordination_status` reports the intake as `unknown`, not as zero saves

### Requirement: Only callers whose saves may queue read queued saves

Only a caller whose saves may be queued SHALL read queued saves or the intake counts; any other caller, including a delegated session of the owner, SHALL receive `QUEUED_WRITE_OWNER_ONLY` from `review_memory(mode="queued-write")`, and `coordination_status` SHALL omit the intake for it.

#### Scenario: A delegated session cannot read queued saves

- **WHEN** a caller with a delegated authorization session of the owner reads `review_memory(mode="queued-write")`
- **THEN** it receives `QUEUED_WRITE_OWNER_ONLY`
