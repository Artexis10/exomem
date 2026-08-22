## ADDED Requirements

### Requirement: Load-Aware Retry Guidance

A retryable busy error SHALL carry a `retry_after_ms` hint that scales with
observed contention, `min(15000, max(750, age_seconds*500))`, at least `5000`
when the boundary is overdue, rather than a fixed value. Once a caller has
computed this hint, later assembly of the error response MUST NOT overwrite
it with a fixed default.

#### Scenario: A longer-held boundary yields a longer retry hint

- **WHEN** a mutation is refused because the boundary has been held
  significantly longer than its configured bound
- **THEN** the returned `retry_after_ms` is larger than the default and at
  least `5000`

#### Scenario: A freshly acquired boundary still yields the floor hint

- **WHEN** a mutation is refused almost immediately after the boundary was
  acquired
- **THEN** the returned `retry_after_ms` is at least `750`

### Requirement: Orphan Holder Snapshot Reports Real Age

When the mutation-lock metadata mutex cannot be acquired quickly, the
snapshot SHALL fall back to a lock-free, tear-free read of the atomically
published holder sidecar and report its real `age_seconds` and `overdue`
with `verified: false`, rather than fabricating an unknown holder at age
zero. An unknown, unverified holder MUST be fabricated only when no sidecar
exists at all.

#### Scenario: A metadata mutex contention still yields a real age

- **WHEN** the metadata mutex cannot be acquired within its short bound but a
  holder sidecar exists
- **THEN** the snapshot reports that holder's real age and `overdue` state
  with `verified: false`

#### Scenario: No sidecar exists

- **WHEN** the metadata mutex cannot be acquired and no holder sidecar exists
- **THEN** the snapshot falls back to an unknown, unverified holder at age
  zero, as before this change

## MODIFIED Requirements

### Requirement: Reads Never Observe A Half-Committed Mutation

Read-only operations SHALL remain available without becoming global
write-queue participants. Consistency for a read is provided structurally:
canonical files land via atomic replace, so a concurrent read observes either
the pre-mutation or post-mutation whole-file state, never a partial write.
Neither local nor hosted read-only operations SHALL acquire the mutation
boundary to obtain this guarantee; a read MUST NOT wait on, weaken, or extend
mutation serialization.

This supersedes the prior hosted design decision that reserved the
consistency-guard bypass to `mode="audit"` and `validate_only` reads while
other hosted reads held the guard
(`openspec/changes/archive/2026-07-20-make-mcp-acknowledgement-replay-safe/design.md:64`):
every hosted read now behaves like a local read, because atomic staging
already provides the guarantee the guard existed to simulate, and holding it
needlessly contended ordinary reads against long-running writes.

#### Scenario: Read overlaps a multi-file write

- **WHEN** a read-only command overlaps a transactional multi-file mutation
- **THEN** its response is assembled from a consistent pre-commit or
  post-commit state
- **AND** it does not expose temporary staging files or partial index/log
  updates

#### Scenario: Hosted read no longer waits behind a long write

- **WHEN** a hosted read-only operation is invoked while another process
  holds the mutation boundary for an unrelated long-running write
- **THEN** the read proceeds immediately without acquiring or waiting for the
  mutation boundary
- **AND** it observes a consistent whole-file pre- or post-commit state, not
  a `MUTATION_BUSY` refusal

#### Scenario: Validation preview overlaps a multi-file write

- **WHEN** a validation-only authoring preview runs while another process holds the mutation boundary
- **THEN** it may return an advisory weak-snapshot draft without waiting for mutation authority
- **AND** the response is non-committed and binds the draft and relevant predecessor inputs
- **AND** a later commit freshly revalidates all mutation and corpus-dependent preconditions under the boundary

### Requirement: Tenant-Scoped Retry And Idempotency Semantics

Hosted mutations SHALL preserve caller-supplied idempotency keys and bounded
implicit MCP retry replay through the existing common invocation boundary.
Retry identity MUST include the resolved tenant, authenticated principal
scope, command, and canonical arguments; a key or implicit retry from one
tenant MUST NOT replay or suppress a mutation for another tenant. Failed
mutations MUST remain retryable.

A `pending` idempotency row whose owning process is provably no longer alive
(an exclusive per-process OS lock file under the state directory is not held)
MUST transition to `abandoned` rather than blocking every future retry
indefinitely. A caller retrying against an abandoned row MUST receive
`OpError("MUTATION_OUTCOME_UNKNOWN", ..., details={status: "uncertain",
committed: None})` (HTTP 409 where applicable), and an identical retry MUST be
allowed to execute fresh no sooner than 60 seconds after abandonment. A
liveness probe error MUST be treated as "alive" (fail closed). A legacy row
with no recorded owner MUST be honored under the prior any-pending-blocks
rule for up to 600 seconds before it, too, ages into `abandoned`.

#### Scenario: Gateway retries a completed hosted mutation

- **WHEN** the gateway repeats an identical successful mutation for the same
  tenant and authenticated principal with the same idempotency identity
- **THEN** the original result is replayed without executing the mutation
  leaf again
- **AND** only one durable vault change exists

#### Scenario: Same key is presented for another tenant

- **WHEN** two tenant contexts present the same explicit idempotency key for
  otherwise identical input
- **THEN** each tenant resolves an independent idempotency record
- **AND** neither tenant receives the other's result or suppresses the
  other's mutation

#### Scenario: First attempt fails

- **WHEN** a mutation raises before successful completion and the caller
  retries it
- **THEN** the failure is not replayed as a completed result
- **AND** the retry can acquire the boundary and execute normally

#### Scenario: The owning process crashed mid-write

- **WHEN** a retry arrives for a `pending` row whose owner process is no
  longer alive
- **THEN** the row transitions to `abandoned` and the retry receives
  `MUTATION_OUTCOME_UNKNOWN` with `status: "uncertain"` and `committed: None`
- **AND** an identical retry made at least 60 seconds later executes fresh

#### Scenario: A liveness probe is inconclusive

- **WHEN** the liveness probe for a `pending` row's owner cannot determine
  whether the owner is alive
- **THEN** the row is treated as still owned and is not abandoned

### Requirement: Crash And Cancellation Release Mutation Authority Safely

The process-safe boundary SHALL not leave a vault permanently unwritable
after process termination, request cancellation, or an exception. Authority
MUST be released automatically when the owning process exits and in a
`finally`-equivalent path for handled cancellation or failure; a successor
MUST still pass normal readiness and transactional integrity checks before
writing.

A writer-lease holder that is not configured as the preferred writer MUST
also release its lease authority after a configurable idle period
(`EXOMEM_WRITER_LEASE_IDLE_SECONDS`, default 60 seconds; `0` disables idle
release) during which it held no in-flight mutation, so authority can return
to another replica without an operator manually intervening. A preferred
writer MUST be exempt from idle release. Idle release MUST NOT clear
authority while a mutation is in flight, and any straggler write that was
already authorized before release completes MUST still be rejected by fencing
if the lease has moved on by the time it reaches the atomic-write boundary.

#### Scenario: Process exits while holding the boundary

- **WHEN** a tenant-cell process terminates while it owns the vault mutation
  boundary
- **THEN** the operating-system or coordination primitive releases that
  ownership without a manual stale-lock edit
- **AND** a replacement cell can acquire the boundary after its readiness
  checks succeed

#### Scenario: Mutation raises an exception

- **WHEN** a command leaf raises while the current process owns the boundary
- **THEN** the boundary is released after rollback and error handling finish
- **AND** a later valid mutation is not permanently blocked

#### Scenario: A non-preferred holder sits idle

- **WHEN** a non-preferred replica holds the writer lease with no in-flight
  mutation for at least the configured idle period
- **THEN** it releases its lease authority
- **AND** another replica can subsequently acquire writer authority without
  operator intervention

#### Scenario: A preferred holder never self-releases from idleness

- **WHEN** a replica configured as the preferred writer holds the lease with
  no in-flight mutation for longer than the idle period
- **THEN** it retains writer authority

#### Scenario: Idle release never fires mid-mutation

- **WHEN** a replica has at least one in-flight mutation
- **THEN** the idle-release check does not clear its lease authority
  regardless of how long it has been since the last mutation started

#### Scenario: A straggler write is fenced after idle release

- **WHEN** a mutation was authorized before idle release cleared the local
  token, and it reaches the atomic-write boundary after another replica has
  acquired a newer fencing token
- **THEN** the straggler write is rejected rather than committed
