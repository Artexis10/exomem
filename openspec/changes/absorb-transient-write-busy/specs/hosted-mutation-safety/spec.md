## MODIFIED Requirements

### Requirement: Lock Acquisition Fails Closed Before Mutation

The system SHALL acquire the vault mutation boundary before invoking a write-capable command leaf or committing an upload. If safe ownership cannot be obtained within the configured bound, the operation MUST fail with a stable machine-readable busy or unavailable error before creating, modifying, moving, or deleting any vault or sidecar file.

A direct `/upload` commit that finds the boundary held by a holder still inside its allowance SHALL wait it out within the request's budget, under the same arrival-order queue, waiter cap and refusal causes (`holder_overdue`, `capture_wait_exhausted`, `capture_waiters_full`) as a keyed command write, because the handler has read or written no vault byte before it holds the boundary. A tus upload session whose commit ends retryable with a busy or warming code SHALL be committed again by the server, with backoff and before the session expires, without waiting for the client's next read of the session.

#### Scenario: Lock wait exceeds its bound

- **WHEN** a mutation cannot acquire the vault boundary before its wait deadline
- **THEN** the operation returns a stable retryable error
- **AND** its command leaf does not execute and the vault remains unchanged

#### Scenario: Lock backend cannot prove ownership

- **WHEN** the process-safe locking primitive is unavailable or returns an indeterminate ownership result
- **THEN** hosted mutation readiness fails closed
- **AND** no write is attempted under assumed ownership

#### Scenario: A direct upload meets an ordinary busy boundary

- **WHEN** a `/upload` commit finds the boundary held by a holder still inside its allowance
- **AND** the holder releases within the request's wait budget
- **THEN** the upload commits once and the client receives its success response
- **AND** no busy refusal reaches the client

#### Scenario: A tus commit stays busy past its own wait

- **WHEN** the commit of a fully received tus upload session ends retryable with a busy or warming code
- **THEN** the server commits the session again with backoff while the session has not expired
- **AND** the session reaches `committed` without a further client request
- **AND** a client that reads the session meanwhile sees the retryable state and its code

### Requirement: Tenant-Scoped Retry And Idempotency Semantics

All MCP mutations, including hosted mutations, SHALL preserve caller-supplied idempotency keys and bounded implicit retry replay through the existing common invocation boundary. Retry identity MUST include the resolved vault or tenant, authenticated principal scope, command, and canonical arguments. A key or implicit retry from one tenant MUST NOT replay or suppress a mutation for another tenant. An identical pending retry MUST inspect or wait on its receipt outside the exclusive vault mutation boundary, while different identities remain subject to normal serialization. Failed precommit mutations MUST become retryable, and committed terminal outcomes MUST replay without executing the leaf again.

A REST mutation without an `Idempotency-Key` header SHALL carry no replay identity: two identical requests are two writes, and a busy boundary refuses it with a retryable `MUTATION_BUSY` after one bounded acquire wait, without waiting out contention, because a client that gave up during a wait and resent it would otherwise commit it twice. A REST client that wants ordinary contention waited out sends one key per intended write; the bundled Studio SHALL send a distinct key with every action, so a repeated action, such as a re-selected folder or a re-run plan, executes again. An in-process CLI or terminal UI mutation without an explicit key SHALL wait out ordinary contention inside the invocation without any replay identity: it creates no replay receipt, it is never offered another invocation's result, and a gate that requires a reusable identity, such as a vocabulary application, still refuses it. A command that issues a credential SHALL carry no implicit identity on any surface, so its terminal never enters the replay store.

A replay identity REQUIRES either an explicit caller-supplied idempotency key or a retry scope that is stable across the retry. Where neither resolves for a remote request — an unauthenticated caller, an unresolved or most-restrictive principal, or a stateless transport whose session identifier changes per request — the repeat carries no replay identity, and the system SHALL treat the two requests as distinct by definition: no shared receipt is created, no replay is offered, and normal serialization applies, up to and including `MUTATION_BUSY` for the second request. Such a request SHALL NOT wait out contention, because a client that gave up during the wait and resent it would commit it twice. The replay guarantee above is therefore explicitly out of scope for that configuration rather than silently unmet.

#### Scenario: Gateway retries a completed hosted mutation

- **WHEN** the gateway repeats the same successful mutation for the same tenant, principal, command, canonical arguments, and idempotency identity after losing the acknowledgement
- **THEN** the original result is replayed without executing the mutation leaf again
- **AND** only one durable vault change exists

#### Scenario: Identical retry overlaps in-flight mutation

- **WHEN** an identical retry arrives while the first worker still owns the mutation boundary
- **THEN** it waits on or inspects the matching pending receipt outside the boundary
- **AND** it returns the terminal replay or bounded `MUTATION_ACKNOWLEDGEMENT_PENDING`, never `MUTATION_BUSY` caused by competing with itself

#### Scenario: Retry arrives with no key and no stable scope

- **WHEN** a remote caller repeats a mutation with no explicit idempotency key while no stable retry scope resolves for it
- **THEN** the repeat resolves no replay identity, creates no shared receipt, and is not offered the first request's result
- **AND** it contends for the vault mutation boundary like any other distinct mutation, up to and including `MUTATION_BUSY`

#### Scenario: A Studio action meets an ordinary busy boundary

- **WHEN** a Studio action finds the boundary held by a holder still inside its allowance
- **AND** the holder releases within the request's wait budget
- **THEN** the action commits once and the Studio shows its result
- **AND** no `MUTATION_BUSY` reaches the Studio

#### Scenario: A repeated Studio action executes again

- **WHEN** a Studio user selects a folder, changes the selection, and selects the folder again
- **THEN** each action is its own write and the stored selection is the last one sent

#### Scenario: An unkeyed REST write meets a busy boundary

- **WHEN** a REST mutation without an `Idempotency-Key` header finds the boundary busy
- **THEN** it returns the retryable `MUTATION_BUSY` after one bounded acquire wait, without waiting out contention, and nothing is written

#### Scenario: A CLI write waits out contention without replaying another invocation

- **WHEN** an in-process CLI mutation without `EXOMEM_IDEMPOTENCY_KEY` finds the boundary held by a holder still inside its allowance
- **THEN** it waits inside the invocation and commits once
- **AND** it leaves no replay receipt, and a later identical CLI invocation executes as a new write

#### Scenario: An unkeyed CLI vocabulary application is refused

- **WHEN** an in-process CLI vocabulary application arrives without `EXOMEM_IDEMPOTENCY_KEY`
- **THEN** it is refused before any state is bound, because it carries no identity a retry can reuse

#### Scenario: Same key is presented for another tenant

- **WHEN** two tenant contexts present the same explicit idempotency key for otherwise identical input
- **THEN** each tenant resolves an independent idempotency record
- **AND** neither tenant receives the other's result or suppresses the other's mutation

#### Scenario: First attempt fails before commit

- **WHEN** a mutation raises during structural or semantic preflight before successful completion and the caller retries it
- **THEN** the pending receipt is removed or records a retryable precommit outcome
- **AND** the retry can acquire the boundary and execute normally

#### Scenario: First attempt fails

- **WHEN** a mutation raises before successful completion and the caller retries it
- **THEN** the failure is not replayed as a completed result
- **AND** the retry can acquire the boundary and execute normally

## ADDED Requirements

### Requirement: Background Boundary Holders Step Aside For Waiters In Any Process

Background work that holds the vault mutation boundary across several units, such as graph republication, derived row steps and collection store ticks, SHALL end its hold at its next unit boundary when a request waits for that boundary, whether the waiting request runs in the holder's process or in another process on the same host. A waiting process SHALL signal its wait through an advisory lock beside the boundary's own lock file. The signal SHALL clear when the process stops waiting, acquires the boundary or exits, so a crashed waiter leaves no signal behind. On a platform without shared advisory locks the signal MAY cover only waiters in the holder's own process. Each unit SHALL still complete before the holder checks for waiters, so background work keeps making progress.

#### Scenario: A write in another process waits while background work holds the boundary

- **WHEN** a process holds the boundary for background work that runs in units
- **AND** a write in another process on the same host waits for the boundary
- **THEN** the background work releases the boundary at its next unit boundary
- **AND** the waiting write acquires the boundary within its wait bound

#### Scenario: A waiter that exits leaves no signal

- **WHEN** a process that waited for the boundary gives up, acquires it, or exits
- **THEN** background holders no longer see a waiter from that process
- **AND** they continue their units without stepping aside for it
