# hosted-mutation-safety Specification

## Purpose

Define one process-safe, vault-scoped mutation boundary shared by every command surface, transfer path, and background writer.
## Requirements
### Requirement: One Process-Safe Mutation Boundary Per Vault

The system SHALL serialize every operation that can modify a vault's canonical Markdown, media, governed indexes, logs, or mutation-owned runtime state through one process-safe boundary keyed by the vault's canonical identity. MCP, REST, CLI, transfer routes, and background workers MUST NOT maintain independent write locks or bypass that boundary.

#### Scenario: Concurrent commands from different product surfaces

- **WHEN** MCP and REST submit write-capable commands against the same vault at the same time
- **THEN** at most one command executes its mutation section at a time
- **AND** both commands reach the same existing command leaves after acquiring the shared boundary

#### Scenario: Separate processes target the same vault

- **WHEN** two Exomem processes resolve different path spellings to the same canonical vault and attempt mutations concurrently
- **THEN** they contend on the same process-safe vault boundary
- **AND** they cannot both enter their mutation sections

### Requirement: Independent Vaults Do Not Share A Lock

The mutation boundary SHALL be scoped to canonical vault identity rather than process-global state. A mutation in one tenant cell MUST NOT serialize or block a mutation in another tenant cell whose vault identity is different.

#### Scenario: Two tenants mutate concurrently

- **WHEN** tenant A and tenant B submit writes against their physically separate vaults
- **THEN** both mutations can proceed concurrently subject to their own vault boundaries
- **AND** neither tenant's lock state, arguments, result, or error is visible to the other

### Requirement: Lock Acquisition Fails Closed Before Mutation

The system SHALL acquire the vault mutation boundary before invoking a write-capable command leaf or committing an upload. If safe ownership cannot be obtained within the configured bound, the operation MUST fail with a stable machine-readable busy or unavailable error before creating, modifying, moving, or deleting any vault or sidecar file.

#### Scenario: Lock wait exceeds its bound

- **WHEN** a mutation cannot acquire the vault boundary before its wait deadline
- **THEN** the operation returns a stable retryable error
- **AND** its command leaf does not execute and the vault remains unchanged

#### Scenario: Lock backend cannot prove ownership

- **WHEN** the process-safe locking primitive is unavailable or returns an indeterminate ownership result
- **THEN** hosted mutation readiness fails closed
- **AND** no write is attempted under assumed ownership

### Requirement: Mutation Boundary Composes With Transactional Writes

The shared boundary SHALL enclose the full observable mutation, including canonical file changes, index and log updates, and mutation-owned sidecar notifications. Existing transactional write and rollback semantics MUST remain in force inside the boundary, and nested write helpers invoked by one command MUST NOT deadlock by attempting to become a competing mutation.

#### Scenario: Multi-file mutation succeeds

- **WHEN** a governed write updates a note, indexes, and the activity log
- **THEN** the command retains the vault boundary until the complete transactional batch and its mutation-owned notifications finish
- **AND** a waiting mutation can begin only after that boundary is released

#### Scenario: Transactional batch rolls back

- **WHEN** a caught failure causes an existing transactional write to restore its pre-write state
- **THEN** the rollback completes while the same mutation boundary is still held
- **AND** the next mutation cannot observe the partially committed state

### Requirement: Transfer And Background Writers Participate

Upload finalization, media extraction that writes canonical or derived artifacts, file-watcher reconciliation, index maintenance, and any other background writer SHALL use the same vault mutation boundary before changing vault-owned state. In hosted-cell mode, an optional worker MUST remain disabled or report unavailable until it can participate safely in that boundary.

#### Scenario: Upload races a governed command

- **WHEN** an upload is ready to commit while a governed command is mutating the same vault
- **THEN** upload finalization waits for the shared vault boundary
- **AND** it cannot interleave its canonical file, sidecar, or log updates with the command

#### Scenario: Unsafe optional worker is configured

- **WHEN** hosted-cell startup enables an optional writer that cannot use the shared mutation boundary
- **THEN** readiness reports that worker unavailable and does not start it
- **AND** durable capture and non-worker core operations remain available where their own safety checks pass

### Requirement: Reads Never Observe A Half-Committed Mutation

Ordinary read-only operations SHALL remain available without becoming global write-queue participants, but they MUST observe either the state before a transactional mutation or the state after it, never a known intermediate batch state. A read MUST NOT weaken mutation serialization or cause a writer to release its boundary early.

Validation-only authoring previews are an explicit exception: they MAY read a weak snapshot without acquiring mutation authority, MUST identify themselves as advisory and non-committed, and MUST bind their exact draft and relevant predecessor inputs. Any later commit MUST reacquire mutation authority and freshly revalidate predecessor hashes, draft identity, writer authority, and corpus-dependent semantic checks; the preview itself MUST NOT be presented as a consistent current-vault snapshot.

#### Scenario: Read overlaps a multi-file write

- **WHEN** an ordinary read-only command overlaps a transactional multi-file mutation
- **THEN** its response is assembled from a consistent pre-commit or post-commit state
- **AND** it does not expose temporary staging files or partial index/log updates

#### Scenario: Validation preview overlaps a multi-file write

- **WHEN** a validation-only authoring preview runs while another process holds the mutation boundary
- **THEN** it may return an advisory weak-snapshot draft without waiting for mutation authority
- **AND** the response is non-committed and binds the draft and relevant predecessor inputs
- **AND** a later commit freshly revalidates all mutation and corpus-dependent preconditions under the boundary

### Requirement: Tenant-Scoped Retry And Idempotency Semantics

Hosted mutations SHALL preserve caller-supplied idempotency keys and bounded implicit MCP retry replay through the existing common invocation boundary. Retry identity MUST include the resolved tenant, authenticated principal scope, command, and canonical arguments; a key or implicit retry from one tenant MUST NOT replay or suppress a mutation for another tenant. Failed mutations MUST remain retryable.

#### Scenario: Gateway retries a completed hosted mutation

- **WHEN** the gateway repeats an identical successful mutation for the same tenant and authenticated principal with the same idempotency identity
- **THEN** the original result is replayed without executing the mutation leaf again
- **AND** only one durable vault change exists

#### Scenario: Same key is presented for another tenant

- **WHEN** two tenant contexts present the same explicit idempotency key for otherwise identical input
- **THEN** each tenant resolves an independent idempotency record
- **AND** neither tenant receives the other's result or suppresses the other's mutation

#### Scenario: First attempt fails

- **WHEN** a mutation raises before successful completion and the caller retries it
- **THEN** the failure is not replayed as a completed result
- **AND** the retry can acquire the boundary and execute normally

### Requirement: Crash And Cancellation Release Mutation Authority Safely

The process-safe boundary SHALL not leave a vault permanently unwritable after process termination, request cancellation, or an exception. Authority MUST be released automatically when the owning process exits and in a `finally`-equivalent path for handled cancellation or failure; a successor MUST still pass normal readiness and transactional integrity checks before writing.

#### Scenario: Process exits while holding the boundary

- **WHEN** a tenant-cell process terminates while it owns the vault mutation boundary
- **THEN** the operating-system or coordination primitive releases that ownership without a manual stale-lock edit
- **AND** a replacement cell can acquire the boundary after its readiness checks succeed

#### Scenario: Mutation raises an exception

- **WHEN** a command leaf raises while the current process owns the boundary
- **THEN** the boundary is released after rollback and error handling finish
- **AND** a later valid mutation is not permanently blocked

### Requirement: Local Single-Vault Compatibility

The mutation-safety capability SHALL preserve existing single-vault MCP, REST, and CLI schemas, result envelopes, governed write behavior, and default startup. Adding the common boundary MUST NOT require hosted configuration, a network coordinator, or a new external service for an ordinary local installation.

#### Scenario: Existing local installation starts

- **WHEN** Exomem starts without hosted-cell configuration
- **THEN** its current single-vault commands and public schemas remain available unchanged
- **AND** local mutations gain serialization without requiring hosted credentials or infrastructure

### Requirement: Mutation Status Measures The Process-Safe Boundary

Coordination status for a concrete vault SHALL probe the same process-safe OS boundary used by mutations rather than relying only on process-local holder memory. Holder publication, lock probing, stale cleanup, and release MUST use a process-safe metadata mutex with one bounded lock order so metadata is bound to the current mutation-lock generation. While another process owns that boundary, status and a timed-out waiter MUST report `held` with bounded content-free verified holder metadata when available, or an explicit unknown unverified external holder when it is not; they MUST NOT report `free`.

#### Scenario: Another process owns the vault boundary

- **WHEN** one process holds the mutation boundary and a second process requests coordination status for the same canonical vault
- **THEN** status reports `held` and never `free`
- **AND** a timed-out mutation returns the same bounded holder identity without vault content or credentials

#### Scenario: New holder pauses before publication

- **WHEN** a process acquires the mutation lock and pauses before publishing holder metadata
- **THEN** status cannot read or attribute the prior generation's metadata
- **AND** after the metadata mutex is released it reports the new verified holder or an explicit unknown unverified holder, never stale verified identity

#### Scenario: A crashed process leaves metadata behind

- **WHEN** holder metadata exists but a nonblocking OS-lock probe succeeds
- **THEN** status reports `free`
- **AND** stale metadata cannot keep the vault unavailable or impersonate an active holder

#### Scenario: Probe cleanup overlaps a new acquirer

- **WHEN** status successfully probes a free mutation lock while stale metadata exists and a writer begins acquisition concurrently
- **THEN** status clears the stale metadata before releasing the mutation lock and metadata mutex
- **AND** it cannot delete metadata published by the new holder

### Requirement: Configured Mutation Wait Bound Is Effective

The default lease manager SHALL use the validated `LeaseConfig.mutation_timeout_seconds` value for process-safe boundary acquisition. Explicit constructor overrides MAY be used by tests and embedded callers, but the environment-derived `EXOMEM_MUTATION_TIMEOUT` MUST NOT be silently ignored.

#### Scenario: Operator configures a non-default timeout

- **WHEN** `EXOMEM_MUTATION_TIMEOUT` resolves to a valid value different from the default and the default manager is created
- **THEN** its mutation coordinator uses that configured acquisition bound

### Requirement: Remote And Local Coordination Remain Separate

Multi-host self-hosting SHALL continue using the strongly consistent writer lease to select one writable replica, while each selected replica and each hosted tenant cell SHALL use the local process-safe boundary to serialize its own service and background-worker processes. A local holder sidecar MUST remain local runtime state and MUST NOT be treated as a cross-machine lease or synced vault content.

#### Scenario: Two machines serve one synchronized vault

- **WHEN** desktop and laptop replicas share one vault through file replication
- **THEN** the remote writer lease permits only its current holder to mutate
- **AND** the current holder's service and worker processes serialize through their local OS boundary

#### Scenario: Hosted tenants use isolated cells

- **WHEN** two hosted tenants mutate physically isolated vault cells
- **THEN** they do not share a local boundary
- **AND** writers inside each individual cell still serialize through that cell's process-safe boundary

### Requirement: Media Work Uses Bounded Per-Artifact Commit Guards

Watcher, startup, and mutation-classified `process_media(process|retry)` discovery scans and provenance hashing SHALL NOT hold the global vault mutation boundary. Those routes MUST acquire a named writer-fenced boundary per artifact only for live binary/access/confinement/content revalidation and canonical sidecar or durable-job mutation. Standard Markdown sidecar fanout after reconciliation, transcript, or failure writes MUST run after that guard is released. Pathless scans MUST NOT wrap the whole scan in one boundary, and explicit process/retry calls MUST retain their existing idempotency identity and writer-lease refusal behavior. This requirement does not claim that bounded CLIP-vector commits or scene-frame persistence are lock-free.

#### Scenario: Foreground mutation overlaps media provenance hashing

- **WHEN** watcher, startup, or explicit media processing is blocked while hashing a large governed binary
- **THEN** a foreground mutation can acquire and release the same vault boundary
- **AND** media processing later reacquires a named per-artifact guard and revalidates before committing

#### Scenario: Background media commit refreshes derived indexes

- **WHEN** a background reconciliation writes or repairs a canonical media sidecar
- **THEN** exact sidecar/job mutation occurs under the per-artifact guard
- **AND** the resulting derived index fanout occurs after that guard is released
