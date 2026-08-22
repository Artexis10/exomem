## ADDED Requirements

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

## MODIFIED Requirements

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
