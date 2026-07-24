## ADDED Requirements

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
- **THEN** it resolves under the host-local state directory, not under the vault tree
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
