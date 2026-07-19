## MODIFIED Requirements

### Requirement: Transfer And Background Writers Participate

Upload finalization, media extraction that writes canonical or derived artifacts, file-watcher reconciliation, index maintenance, and any other background writer SHALL use the same vault mutation boundary before changing vault-owned state. Background work MUST acquire the boundary only for a bounded item or batch and MUST release it between bounds so an optional backlog cannot monopolize interactive mutations. In hosted-cell mode, an optional worker MUST remain disabled or report unavailable until it can participate safely in that boundary.

#### Scenario: Upload races a governed command

- **WHEN** an upload is ready to commit while a governed command is mutating the same vault
- **THEN** upload finalization waits for the shared vault boundary
- **AND** it cannot interleave its canonical file, sidecar, or log updates with the command

#### Scenario: Unsafe optional worker is configured

- **WHEN** hosted-cell startup enables an optional writer that cannot use the shared mutation boundary
- **THEN** readiness reports that worker unavailable and does not start it
- **AND** durable capture and non-worker core operations remain available where their own safety checks pass

#### Scenario: Reconciliation backlog is large

- **WHEN** file-watcher or media reconciliation discovers more work than one configured mutation batch
- **THEN** it commits at most the bounded batch while holding the vault boundary and releases ownership before the next batch
- **AND** an admitted interactive mutation can contend between batches

### Requirement: Tenant-Scoped Retry And Idempotency Semantics

All MCP mutations, including hosted mutations, SHALL preserve caller-supplied idempotency keys and bounded implicit retry replay through the existing common invocation boundary. Retry identity MUST include the resolved vault or tenant, authenticated principal scope, command, and canonical arguments. An identical pending retry MUST inspect or wait on its receipt outside the exclusive vault mutation boundary, while different identities remain subject to normal serialization. Failed precommit mutations MUST become retryable, and committed terminal outcomes MUST replay without executing the leaf again.

#### Scenario: Gateway retries a completed hosted mutation

- **WHEN** the gateway repeats the same successful mutation for the same tenant, principal, command, canonical arguments, and idempotency identity after losing the acknowledgement
- **THEN** the original result is replayed without executing the mutation leaf again
- **AND** only one durable vault change exists

#### Scenario: Identical retry overlaps in-flight mutation

- **WHEN** an identical retry arrives while the first worker still owns the mutation boundary
- **THEN** it waits on or inspects the matching pending receipt outside the boundary
- **AND** it returns the terminal replay or bounded `MUTATION_ACKNOWLEDGEMENT_PENDING`, never `MUTATION_BUSY` caused by competing with itself

#### Scenario: Same key is presented for another tenant

- **WHEN** two tenant contexts present the same explicit idempotency key for otherwise identical input
- **THEN** each tenant resolves an independent idempotency record
- **AND** neither tenant receives the other's result or suppresses the other's mutation

#### Scenario: First attempt fails before commit

- **WHEN** a mutation raises during structural or semantic preflight before successful completion and the caller retries it
- **THEN** the pending receipt is removed or records a retryable precommit outcome
- **AND** the retry can acquire the boundary and execute normally

### Requirement: Crash And Cancellation Release Mutation Authority Safely

The process-safe boundary SHALL not leave a vault permanently unwritable after process termination, request cancellation, or an exception. Authority MUST be released automatically when the owning process exits and in a `finally`-equivalent path for handled cancellation or failure. Transport cancellation MUST NOT erase or misclassify the terminal state of underlying synchronous work that continues in a worker thread. A successor MUST still pass normal readiness and transactional integrity checks before writing.

#### Scenario: Process exits while holding the boundary

- **WHEN** a tenant-cell process terminates while it owns the vault mutation boundary
- **THEN** the operating-system or coordination primitive releases that ownership without a manual stale-lock edit
- **AND** a replacement cell can acquire the boundary after its readiness checks succeed

#### Scenario: Mutation raises an exception

- **WHEN** a command leaf raises while the current process owns the boundary
- **THEN** the boundary is released after rollback and error handling finish
- **AND** a later valid mutation is not permanently blocked

#### Scenario: Transport response is cancelled while worker continues

- **WHEN** the client disconnects or cancels after a synchronous mutation worker has started
- **THEN** the worker finishes or fails under the same boundary and records its terminal receipt before releasing ownership
- **AND** an identical retry observes that receipt rather than executing a duplicate mutation

## ADDED Requirements

### Requirement: Mutation Holder State Is Observable Without Content
The mutation coordinator SHALL track a bounded content-free holder snapshot containing an opaque request identifier, operation name, holder kind, acquisition time, and age. Readiness or coordination diagnostics SHALL report whether the boundary is free, held, or over its long-holder threshold without exposing arguments, paths, entity names, note titles, content, credentials, or principal identity.

#### Scenario: Interactive mutation is held too long
- **WHEN** one mutation remains inside the boundary beyond the configured long-holder threshold
- **THEN** logs and diagnostics expose its opaque request ID, operation, holder kind, and age with a warning state
- **AND** the system does not revoke the live owner or expose vault content

#### Scenario: Boundary is free
- **WHEN** no operation owns or is acquiring the vault mutation boundary
- **THEN** diagnostics report a free state with no stale holder metadata
