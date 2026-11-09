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

A direct `/upload` commit that finds the boundary held by a holder still inside its allowance SHALL wait it out within the request's budget, under the same arrival-order queue, waiter cap and refusal causes (`holder_overdue`, `capture_wait_exhausted`, `capture_waiters_full`) as a keyed command write, because the handler has read or written no vault byte before it holds the boundary. A source capture of client artifacts SHALL wait out the boundary the same way, because it stages its bytes outside the vault before it holds the boundary, and its body SHALL run once. A tus upload session whose commit ends retryable with a busy or warming code SHALL be committed again by the server, with backoff and before the session expires, without waiting for the client's next read of the session. A background session commit waits in line like any keyed write but takes no waiter slot, because the waiter cap protects request workers and a commit thread is not one.

A credential issuance (`transfer_artifact` upload or download, and `govern_memory` session open or rotate) SHALL NOT take the vault mutation boundary and SHALL NOT be refused with `MUTATION_BUSY`, because it writes no vault-owned file: it mints a token over a private store or copy, or writes only its own authorization-session transaction.

A write whose server-planned navigation auxiliary (index, subindex or log) changed while it waited for the boundary SHALL be run again by the server within its wait, or refused with a retryable `MUTATION_BUSY` whose `cause` is `auxiliary_changed` for a caller that does not wait. It SHALL NOT be refused with `STALE_SEMANTIC_WRITE`. The refusal needs positive evidence that an auxiliary's guard no longer holds. A write whose own basis changed (its page, source, vocabulary or retained input) keeps its staleness error, and a refusal with no moved guard keeps its own error and is not run again.

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

#### Scenario: A source capture meets a boundary held by another capture

- **WHEN** a `capture_source` or `preserve_artifacts` commit finds the boundary held by another capture still inside its allowance
- **AND** the holder releases within the capture's wait budget
- **THEN** the source capture stores its file once and the client receives its stored outcome
- **AND** no busy refusal reaches the client

#### Scenario: A boundary stays held past the wait at one file of a batch

- **WHEN** a multi-file `capture_source` or `preserve_artifacts` batch finds the boundary held past the wait at file k
- **THEN** file k ends as `failed` with the retryable code `MUTATION_BUSY`, and each later file is tried on its own
- **AND** every earlier file keeps its stored outcome and the batch ends committed-partial
- **AND** a busy raised after a file's own commit is never reported as that file's failure
- **AND** the batch's identity is settled once any file commits, so a repeat of the identical call replays the batch and does not retry file k
- **AND** the `reason` of file k tells the caller to retry it in a new call that names only the failed files

#### Scenario: A credential issuance meets a held boundary

- **WHEN** another process holds the vault mutation boundary
- **AND** a caller opens or rotates an authorization session, or requests a `transfer_artifact` upload or download
- **THEN** the credential is issued at once
- **AND** no busy refusal reaches the caller

#### Scenario: Navigation changes while a keyed write waits

- **WHEN** an upload rewrites `log.md` and an index after a keyed write planned its navigation and before it acquired the boundary
- **THEN** the write commits once with one log entry and a resend with the same key replays
- **AND** no `STALE_SEMANTIC_WRITE` reaches the caller
- **AND** a write whose own page changed in the same interval still returns its staleness error
- **AND** a write refused because its folder is not writable ends at once with its own error, not with `MUTATION_BUSY`

#### Scenario: A tus commit waits while a request write needs a waiter slot

- **WHEN** a tus session commit is waiting for the boundary and the waiter cap is one
- **THEN** a keyed request write waits and commits instead of refusing with `capture_waiters_full`

#### Scenario: A tus commit stays busy past its own wait

- **WHEN** the commit of a fully received tus upload session ends retryable with a busy or warming code
- **THEN** the server commits the session again with backoff while the session has not expired
- **AND** the session reaches `committed` without a further client request
- **AND** a client that reads the session meanwhile sees the retryable state and its code

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

### Requirement: Hosted Mutation Error Shapes Cover Every Terminal Code
The hosted response layer SHALL carry the structured `status` and `committed` fields for every mutation error code the mutation terminal can emit, including `MUTATION_OUTCOME_UNKNOWN` (`status="uncertain"`, `committed=null`), so a connector client can distinguish never-executed, retryable, committed, and uncertain outcomes without parsing prose.

#### Scenario: Outcome-unknown reaches the hosted client
- **WHEN** a hosted mutation resolves to `MUTATION_OUTCOME_UNKNOWN`
- **THEN** the hosted error carries `status="uncertain"`, `committed=null`, and the same-identity remediation text
- **AND** the ledger row records the code as a refusal

#### Scenario: Shape table and terminal codes stay aligned
- **WHEN** the test suite enumerates the mutation terminal's error codes that carry `status` details
- **THEN** every such code has an entry in the hosted shape table

### Requirement: Mutation Holder State Is Observable Without Content
The mutation coordinator SHALL track a bounded content-free holder snapshot containing an opaque request identifier, operation name, holder kind, acquisition time, and age. Readiness or coordination diagnostics SHALL report whether the boundary is free, held, or over its long-holder threshold without exposing arguments, paths, entity names, note titles, content, credentials, or principal identity.

#### Scenario: Interactive mutation is held too long
- **WHEN** one mutation remains inside the boundary beyond the configured long-holder threshold
- **THEN** logs and diagnostics expose its opaque request ID, operation, holder kind, and age with a warning state
- **AND** the system does not revoke the live owner or expose vault content

#### Scenario: Boundary is free
- **WHEN** no operation owns or is acquiring the vault mutation boundary
- **THEN** diagnostics report a free state with no stale holder metadata

### Requirement: Keyed Writes Wait Out Warm-Up Within Their Request

A keyed governed write that arrives while a write-gating warm-up component is still warming SHALL wait for that component inside its request, within the bounded wait and the parked-worker cap that a busy boundary uses, and SHALL then run once. It SHALL return the retryable warm-up refusal only when that bound runs out first or no wait slot is free, and the refusal SHALL report how long the write waited. An unkeyed write SHALL keep the immediate retryable refusal, because a resend after a client gave up during the wait would run it twice. A write attempted on a collection store's thread SHALL keep it too, because that thread never parks.

#### Scenario: A keyed write during a short warm-up commits

- **WHEN** a keyed governed write arrives while the semantic corpus is warming
- **AND** the corpus becomes ready within the bounded wait
- **THEN** the write runs once and commits
- **AND** the client receives no warm-up refusal

#### Scenario: A warm-up longer than the bound refuses

- **WHEN** a keyed governed write arrives while a write-gating component stays warming past the bounded wait
- **THEN** the write returns the retryable warm-up refusal before its request deadline
- **AND** the refusal reports the time waited
- **AND** the command leaf does not execute

#### Scenario: An unkeyed write is refused at once

- **WHEN** an unkeyed governed write arrives while a write-gating component is warming
- **THEN** it returns the retryable warm-up refusal without waiting

#### Scenario: A store-routed write does not park the store thread

- **WHEN** a keyed governed write routed to a collection store arrives while a write-gating component is warming
- **THEN** it returns the retryable warm-up refusal without waiting on the store thread

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
