# call-ledger Specification

## Purpose
TBD - created by archiving change add-mcp-call-ledger. Update Purpose after archive.
## Requirements
### Requirement: Every MCP Call Produces Exactly One Ledger Row

The system SHALL append exactly one ledger row for each completed MCP tool call — read or
write, success or refusal or error — recorded at the single point where both the call's
duration and its outcome are known. A call SHALL NOT produce zero rows and SHALL NOT produce
more than one row.

#### Scenario: Successful write appends one row

- **WHEN** an MCP write tool call completes normally
- **THEN** exactly one ledger row is appended for that call
- **AND** its `outcome` is `ok`
- **AND** its `request_id` matches the correlation id used by the call tracer

#### Scenario: Read call is recorded too

- **WHEN** an MCP read tool call completes normally
- **THEN** exactly one ledger row is appended with `outcome` of `ok`
- **AND** the row is present even though no mutation and no writer lease were involved

#### Scenario: One call is never double-counted

- **WHEN** a single tool call passes through both the middleware seam and the wrapper seam
- **THEN** only one row is emitted for that call, not one per seam

#### Scenario: A call rejected before the leaf is still recorded

- **WHEN** a call is rejected by a pre-check that runs ahead of the tool leaf — the binary-blob
  content guard, or `edit_memory` operation normalization
- **THEN** exactly one ledger row is appended for that call
- **AND** its `outcome` is `refused` carrying the rejecting check's own code
- **AND** the row records the arguments the client actually sent, before any translation

### Requirement: Refusals Record Their Error Code And Are Distinguishable From Successes

The system SHALL record a governance refusal — a call for which the tool wrapper returns an
error envelope rather than raising — with an `outcome` of `refused` and the refusal's
`OpError.code`. A refused call SHALL NOT be recorded with an `outcome` of `ok`. An uncaught
exception SHALL be recorded with an `outcome` of `error`.

#### Scenario: Writer-lease refusal is recorded as a refusal

- **WHEN** a write is refused because the writer lease is not held
- **THEN** the ledger row has `outcome` of `refused`
- **AND** `error_code` is the refusal's `OpError.code` such as `WRITER_LEASE_REQUIRED`

#### Scenario: Duplicate-identity refusal is not a success

- **WHEN** a write is refused for a duplicate semantic identity
- **THEN** the ledger row has `outcome` of `refused` and `error_code` of
  `SEMANTIC_IDENTITY_DUPLICATE`
- **AND** the row is not recorded as `outcome` of `ok`

#### Scenario: Uncaught exception is recorded as an error

- **WHEN** a tool call raises an exception that the wrapper does not convert to an envelope
- **THEN** the ledger row has `outcome` of `error`
- **AND** `error_code` identifies the exception class rather than being null

### Requirement: Every Row Carries The Call's Latency, Leaf And Total

Every row SHALL carry the latency of the call it records, for reads as well as writes and for
refusals as well as successes: `duration_ms` for the tool leaf, and `total_ms` for the wall
clock the caller actually waited, including work done before the leaf. `duration_ms` SHALL keep
its existing meaning, so that the prose trace and the per-tool duration metric are not silently
redefined. Rows SHALL also record the total serialized size of the arguments.

#### Scenario: A slow call reports how long it took

- **WHEN** a tool call takes appreciable time
- **THEN** the row's `duration_ms` reflects the leaf's own time
- **AND** `total_ms` is at least `duration_ms`

#### Scenario: Time spent before the leaf is visible

- **WHEN** a call spends time in a pre-leaf check — content guarding or argument normalization
- **THEN** that time is reflected in `total_ms`
- **AND** it is not hidden by `duration_ms`, which does not start until the leaf does

#### Scenario: A refusal reports how long the caller waited

- **WHEN** a call is refused after waiting on a contended resource
- **THEN** the row records both the refusal code and the elapsed time
- **AND** "it waited, then was refused" is reconstructable from the row alone

#### Scenario: Request size accompanies the latency

- **WHEN** two calls carry arguments of very different sizes
- **THEN** their rows record correspondingly different `request_bytes`
- **AND** no argument value is recorded to produce that figure

### Requirement: Every Row Names The Calling Client

The system SHALL record, on every row, which MCP client made the call: the client name and
version from the initialize handshake's `clientInfo`, the transport, and the session id. These
SHALL be recorded in the clear, not hashed — they identify software, not a person, and hashing
them would destroy the only field that answers "which client?" while protecting nothing. Absence
of an MCP context SHALL yield null fields rather than an error.

#### Scenario: A row identifies which client called

- **WHEN** a call arrives from a client whose handshake declared a name and version
- **THEN** the ledger row records that client name and version in the clear
- **AND** it records the session id, so one client's calls can be followed as a sequence

#### Scenario: Two clients against one vault are distinguishable

- **WHEN** two different MCP clients call the same vault
- **THEN** their rows carry different `client_name` values
- **AND** the ledger can be filtered to one client's calls without consulting any other source

#### Scenario: No MCP context degrades to null, never to a failure

- **WHEN** a call is recorded outside an MCP session — a CLI invocation or a background drain
- **THEN** the client identity fields are null
- **AND** the row is still appended and the call still succeeds

### Requirement: Arguments Are Recorded As Shape And Hash, Never Values

The system SHALL record call arguments as their names, per-argument byte length and sha256, and
structural target paths only. It SHALL NOT record any argument value, note content, or raw
caller credential. Caller identity SHALL be recorded as the pre-hashed principal scope, never a
raw token or subject.

#### Scenario: Query text never reaches the ledger

- **WHEN** an `ask_memory` call carries free-text query content in its arguments
- **THEN** the ledger row records the argument name, its byte length, and its sha256
- **AND** the query text itself appears nowhere in the row

#### Scenario: Note body is reduced to shape

- **WHEN** a write call carries note body content in its arguments
- **THEN** the body content appears nowhere in the row
- **AND** the addressed note path is recorded structurally in `target_paths`

#### Scenario: Caller identity is a hash, not a credential

- **WHEN** a call is made with a verified principal or a bearer credential
- **THEN** `caller_principal_hash` is the hashed principal scope
- **AND** no raw token, authorization header, or subject value appears in the row

### Requirement: The Ledger Writes On A Read-Only Vault Replica

The ledger SHALL be written to a host-local location outside the vault and outside the
writer-lease mutation boundary, so that a row is recorded even when the vault is a read-only
replica or the writer lease is held elsewhere.

#### Scenario: Refused write on a read-only replica is still recorded

- **WHEN** a call is refused because the vault is read-only or the lease is unavailable
- **THEN** the ledger row for that refusal is appended successfully to the host-local ledger
- **AND** appending the row does not itself require the writer lease or a writable vault

#### Scenario: Ledger location is independent of the vault

- **WHEN** the ledger path is resolved
- **THEN** it resolves under the host-local log directory alongside the existing journals, not
  under the vault tree
- **AND** it is unaffected by the vault being mounted read-only

### Requirement: Rows Carry A Monotonic Sequence And Previous-Row Hash Chain

Each ledger row SHALL carry a strictly increasing `sequence` and a `prev_hash` equal to the
previous row's `row_hash`, and a `row_hash` computed as the hash of the row's canonical form
excluding `row_hash`. Removal, reordering, or edit of any row SHALL be detectable as a sequence
gap or a broken chain.

#### Scenario: Consecutive rows chain correctly

- **WHEN** two rows are appended in order
- **THEN** the second row's `sequence` is greater than the first's
- **AND** the second row's `prev_hash` equals the first row's `row_hash`

#### Scenario: Genesis row uses the defined anchor

- **WHEN** the very first row of a fresh ledger is appended
- **THEN** its `prev_hash` is the defined genesis anchor value
- **AND** its `sequence` is the defined starting value

#### Scenario: A tampered or dropped row is detectable

- **WHEN** a row is later removed or its contents altered
- **THEN** verification detects a gap in `sequence` or a `prev_hash` that no longer matches the
  preceding `row_hash`

### Requirement: Rotation Bounds Size Without Losing Sequence Continuity

The ledger SHALL rotate when the live file exceeds a configured size, moving the oldest rows
beyond a retained newest-N window byte-exact into a content-addressed archive. Rotation SHALL
NOT reset `sequence` and SHALL preserve the hash chain across the archive boundary.

#### Scenario: Oversized live file rotates to an archive

- **WHEN** the live ledger exceeds the configured rotation size
- **THEN** the oldest rows beyond the retained window are moved byte-exact into a
  content-addressed archive file
- **AND** the live file retains the newest rows

#### Scenario: Sequence and chain span the boundary

- **WHEN** rotation moves rows into an archive
- **THEN** `sequence` continues without reset across the boundary
- **AND** the archived segment's last `row_hash` equals the live file's first retained row's
  `prev_hash`

### Requirement: The Ledger Is Readable From The Existing Operator Tooling

The ledger SHALL be reachable through the tooling an operator already uses, with no second
surface to learn: it SHALL be a `exomem logs` file alias, it SHALL be one of the sources
`exomem trace <request_id>` joins, and its chain SHALL be checkable by a command. Reads of the
ledger SHALL span the archive and the live file as one sequence.

#### Scenario: A trace includes the ledger

- **WHEN** an operator traces a request id
- **THEN** the ledger's row for that request appears in the joined, time-ordered output
- **AND** it appears even for a tool that is neither a read, a query, nor a mutation, which no
  other source records

#### Scenario: Reading the ledger spans rotation

- **WHEN** the ledger has rotated and an operator reads or traces it
- **THEN** rows from the archived segments are included alongside the live file
- **AND** they are ordered by the sequence the rows carry, not by archive filename, which is
  content-addressed and says nothing about age

#### Scenario: The chain is checkable by a command

- **WHEN** an operator runs the ledger verification command
- **THEN** an intact chain reports no problems and exits zero
- **AND** a dropped, reordered, or edited row is reported with the row it was detected at
- **AND** a dropped *oldest* segment is reported, because the walk is anchored to the genesis
  row rather than merely appearing to start later than it did

### Requirement: A Ledger Write Never Breaks Or Slows The Call

A failure to build, hash, append, or rotate a ledger row SHALL be contained and SHALL NOT raise
into the tool-call path or change the call's result. The append SHALL add negligible latency and
SHALL NOT fsync on the hot path.

#### Scenario: A ledger failure is contained

- **WHEN** the ledger cannot be written because of a full disk or a permission error
- **THEN** the tool call still returns its normal result
- **AND** the failure is swallowed rather than propagated to the caller

#### Scenario: Append does not fsync on the hot path

- **WHEN** a row is appended during a normal call
- **THEN** the append does not perform an fsync in the call path
- **AND** any rows lost to a hard crash are detectable as a later `sequence` gap

### Requirement: A CI Test Asserts No Note Content Reaches The Ledger

The change SHALL add a continuous-integration test that drives calls carrying known sentinel
content and asserts that no such content, and no raw credential, appears anywhere in the ledger
file. The test SHALL also assert the ledger file's restrictive permission where the platform
supports it.

#### Scenario: Sentinel content never appears in the ledger

- **WHEN** the test drives a call whose arguments contain a unique sentinel string
- **THEN** the sentinel string appears nowhere in the ledger file or its archives
- **AND** the argument is instead represented by its name, length, and sha256

#### Scenario: Ledger file permission is restrictive

- **WHEN** the ledger file is created on a platform that supports file modes
- **THEN** its mode is restrictive such as `0600`
