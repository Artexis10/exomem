## ADDED Requirements

### Requirement: Canonical mutations use non-owned lifecycle states and exact receipts

Each namespaced idempotent mutation SHALL use only `reserved`, `executing`,
`canonically_committed`, and `completed` as its durable canonical lifecycle
states. Execution ownership SHALL be a bounded per-claim attempt, not a durable
state owner. A dead `reserved` row SHALL be reclaimable. A dead `executing` row
without valid authenticated evidence SHALL be terminally `outcome_unknown` and
SHALL NOT be reclaimed for leaf execution.

A claim SHALL receive a lowercase 24-hex `commit_token` and fresh private
32-byte per-attempt secret. It SHALL retain one atomically installed, hidden,
portable, content-free `GraphCommitReceipt` v2 for at least the retry-row
lifetime. Receipt v2 SHALL bind the namespaced idempotency-key digest,
command/payload digest, attempt/claim and commit token, canonical terminal
projection, and exact checkpoint generation/digest, and SHALL authenticate its
canonical v2 bytes with HMAC-SHA256 using that secret. The secret SHALL exist
only in the matching owner-only local idempotency runtime state, never in a
synced vault artifact, log, terminal, or diagnostic. The local runtime directory
SHALL be owner-only (`0700`); its database, WAL/SHM files, and secret material
SHALL be `0600` or stricter. On Windows, the per-attempt secret SHALL exist
only in the local SQLite binary BLOB
`EXID | version=1 | provider=1(DPAPI_CURRENT_USER) | uint32be ciphertext_length | ciphertext`.
Total BLOB length and ciphertext length SHALL each be at most 4096 bytes;
`ciphertext_length` SHALL exactly consume the remaining bytes. The parser SHALL
reject truncation, trailing bytes, unknown version/provider, and every other
non-exact encoding. Its entropy SHALL be UTF-8
`exomem-graph-commit-receipt-dpapi:v1\0<attempt_id>\0<commit_token>`; no clear
secret SHALL be serialized. The protected inheritable runtime DACL SHALL permit
only the current service identity, `LocalSystem`, and `Administrators`. Before
SQLite is opened or a runtime row is unpickled, the service SHALL reject a
reparse-point runtime path and an existing unsafe DACL. SQLite, WAL, and SHM
SHALL contain only the DPAPI ciphertext. An account/profile change, copied
envelope, failed DPAPI unprotect, or raw legacy secret row SHALL classify the
claim `outcome_unknown`; none may heal local CAS or authorize leaf replay.

Receipt eligibility SHALL be non-circular. “Complete ordinary protocol
succeeded” SHALL mean, before signing, that: the claimed leaf returned its
candidate terminal; one guarded vault batch successfully applied its selected
floor, caller canonical replacements, and exact checkpoint; and every bounded
canonical fanout precondition needed to select and validate that checkpoint's
incremental-or-rebuild route completed. It SHALL exclude receipt
installation/authentication, local lifecycle CAS, durable derived-handle
registration, `ensure_started`, graph join, and completed-terminal persistence.

`terminal_projection` SHALL be a closed content-free object. Its only permitted
fields are `_terminal`, `version`, `ok`, `state`, `status`, `committed`,
`mutated`, `terminal`, `request_id`, `receipt_id`, `operation_id`,
`warnings_count`, and `result_sha256`; absent permitted fields SHALL be omitted.
Scalar identifiers SHALL use their existing bounded formats and `result_sha256`
SHALL be lowercase SHA-256 of an otherwise non-retained result summary. No
other top-level or nested projection field is permitted. In particular,
`path`, `paths`, `affected_paths`, every vault path or file name, Markdown,
metadata values, source text, and arbitrary leaf result/content SHALL NOT be
stored in the projection. Graph outcome SHALL be attached only after receipt
persistence and SHALL NOT be part of `terminal_projection`.

Receipt v2 SHALL be a closed object with exactly `version`,
`idempotency_key_digest`, `command_digest`, `attempt_id`, `commit_token`,
`terminal_projection`, `terminal_projection_sha256`, `checkpoint_generation`,
`checkpoint_sha256`, `canonical_disposition`, and `receipt_hmac_sha256`.
`version` SHALL be integer `2`; `canonical_disposition` SHALL be exactly
`success` or `committed_failure`; the checkpoint fields SHALL both be null or a
positive integer/lowercase SHA-256 pair; every other digest SHALL be lowercase
SHA-256. `canonical_disposition` SHALL be HMAC-covered and SHALL NOT alter the
closed `terminal_projection` allowlist. Its HMAC input
SHALL be UTF-8 bytes `exomem-graph-commit-receipt-auth:v2\0` concatenated with
canonical UTF-8 JSON of every v2 field except `receipt_hmac_sha256`. Canonical
JSON SHALL have no insignificant whitespace, lexicographically sorted object
keys, and unescaped non-ASCII Unicode. The parser SHALL reject duplicate,
missing, surplus, wrongly typed, or noncanonical fields and a mismatched
terminal-projection digest before authentication. Verification SHALL recompute
HMAC-SHA256 and compare the received digest in constant time.

There SHALL be no mutable `commit_point` field or two-write receipt marker.
Receipt v2 SHALL be authoritative only after the defined pre-signing ordinary
protocol succeeded. It SHALL be atomically installed before the local
CAS from the matching `executing` row to `canonically_committed`. A failed local
CAS after installation MAY be healed only by that exact receipt, matching
trusted executing row, attempt/token binding, and retained private secret.
`graph_pending` SHALL NOT be written by new code. Valid legacy `graph_pending`
is a local-row-only migration to `canonically_committed`; it SHALL never permit
the canonical leaf to replay. Legacy v1 boolean receipts and all synced vault
receipt artifacts are advisory only. Graph health for a canonically committed
local row SHALL be dynamically joinable or recomputable from its exact
checkpoint, independent of the original attempt.

An authenticated orphaned v2 receipt with `canonical_disposition: success` MAY
heal only its matching trusted executing CAS and then continue the ordinary
derived graph path. An authenticated orphan with
`canonical_disposition: committed_failure` SHALL suppress leaf replay; it SHALL
replay the retained exact local failure when available and otherwise persist
`outcome_unknown` while independently recovering graph health. A missing,
invalid, or fieldless v2 receipt SHALL heal neither disposition.

#### Scenario: Dead execution attempt cannot duplicate a committed leaf
- **WHEN** a claim's executing attempt dies after canonical files could change
- **AND** no exact receipt v2 can be authenticated with its trusted local row and secret
- **THEN** exact retry returns `outcome_unknown` rather than reclaiming the leaf

#### Scenario: Installed receipt heals only its matching local CAS
- **WHEN** receipt v2 installs but the matching executing-to-committed CAS fails
- **THEN** recovery heals it only with that exact trusted executing row and retained secret
- **AND** a copied receipt alone cannot suppress or complete a mutation

#### Scenario: Predecessor graph-pending row migrates safely
- **WHEN** a retry reads a valid legacy `graph_pending` row
- **THEN** it treats the row as `canonically_committed` with its validated checkpoint
- **AND** it can join/recompute graph work without replaying the leaf

### Requirement: The multi-store canonical cut remains outcome-unknown

The vault batch and retry-store receipt SHALL NOT be treated as one atomic
transaction. If the process dies after any canonical caller file, floor, or
checkpoint write can have committed but before matching receipt v2 atomically
installs, the exact retry SHALL return the fail-closed `outcome_unknown`
terminal classification with readback guidance. If it dies after v2 installation
but before local CAS, only its matching trusted executing row and retained
private secret may heal the CAS. A committed-attempt cleanup failure SHALL NOT
reconstruct a successful mutation from an orphaned receipt.
`outcome_unknown` is outside the four canonical lifecycle states and SHALL NOT
be treated as proof that the canonical mutation reached either
`canonically_committed` or successful `completed`. The retry store MAY retain
the immutable fail-closed classification in a `completed` row solely as a
terminal storage envelope, so exact retries read back the same classification
without creating a fifth state; that envelope does not assert canonical
completion. The system SHALL NOT rerun the leaf, infer success from filesystem
state, fabricate a canonical terminal, or convert the cut to a receipt.
Checkpoint recovery MAY independently converge graph health. Recovery SHALL
not scan receipt artifacts: it SHALL read only the exact receipt address named
by a trusted local row, under bounded artifact-size and parsing limits. A synced
receipt is untrusted/advisory, and a copied receipt without matching local row
and secret SHALL NOT suppress a mutation. Cross-replica exactly-once is
explicitly deferred until a shared pre-leaf CAS exists.

#### Scenario: Crash after checkpoint before receipt installation
- **WHEN** a process dies after the caller batch published its checkpoint but before matching receipt v2 atomically installs
- **THEN** exact retry returns `outcome_unknown`
- **AND** the canonical leaf invocation count remains one

### Requirement: Canonical batches publish strict graph epochs and receipts in order

Every graph-relevant claimed canonical mutation SHALL select one generation
above the maximum valid floor, checkpoint, and acknowledgement, then execute
`floor → caller files → checkpoint → one authenticated receipt v2 → local CAS`
in that order. Caught failure SHALL roll back the vault batch and SHALL not
install a receipt. Graph-irrelevant writes SHALL advance neither artifact and
create no graph receipt. Setup, floor seeding, reconcile repair, cleanup, and
rollback SHALL use `commit_point=False` and SHALL never mark a mutation
committed; this API flag SHALL NOT be persisted as a receipt marker.

The public `.graph-sync.json` SHALL remain closed v1 with unchanged canonical
bytes and valid digest vectors. Its parser SHALL reject more than 1,000 `paths`
entries before normalization; surplus fields; paths not already NFC-normalized;
unsafe/noncanonical POSIX-relative paths;
absolute, backslash, empty, dot, dotdot, or NUL path components; non-null hashes
other than lowercase 64-hex; conflicting duplicate paths; non-unique or
non-created `created_paths`; and non-empty arrays for `full` scope. The internal
floor SHALL remain closed `{version,generation,floor_sha256}` under its distinct
digest domain.

#### Scenario: Existing checkpoint vector remains stable
- **WHEN** the existing v1 checkpoint test vector is rendered after this change
- **THEN** its bytes and domain-separated digest exactly equal the pinned vector

#### Scenario: Strict parser rejects normalization ambiguity
- **WHEN** a v1 checkpoint contains an equivalent-but-noncanonical path, duplicate conflict, or malformed full scope
- **THEN** parsing fails before it can influence generation, acknowledgement, or recovery

### Requirement: Epoch migration and acknowledgement parity fail closed

Exact legacy is the simultaneous absence of floor, checkpoint, and graph
acknowledgement. The only accepted pre-floor state is a valid checkpoint with
absent floor and either absent acknowledgement or acknowledgement exactly equal
to that checkpoint; mutation authority may seed the floor at that generation.
A valid floor with missing, malformed, or older checkpoint SHALL recover only at
a higher full-scope generation. All other missing/malformed floor or
contradictory acknowledgement combinations SHALL fail closed.

Public graph availability SHALL require exact current checkpoint
generation/digest parity and every currently written acknowledgement metadata
key, including `graph_sync_checkpoint`. Missing metadata SHALL NOT default to
success in this migration. A malformed state requires fresh full recovery, not
incremental admission.

#### Scenario: Missing acknowledgement metadata is unavailable
- **WHEN** a graph sidecar has matching generation/digest but lacks `graph_sync_checkpoint`
- **THEN** graph availability is false and full recovery is required

#### Scenario: Valid floor plus stale checkpoint recovers above the floor
- **WHEN** recovery finds floor generation N and a missing, malformed, or older checkpoint
- **THEN** it publishes a fresh full-scope checkpoint with generation greater than N
- **AND** it does not reuse a generation or alter user-authored bytes

### Requirement: Every committed checkpoint has an exact graph outcome handle

Before a graph-relevant checkpoint leaves canonical writer authority, the system
SHALL either atomically acknowledge it incrementally, register an exact durable
rebuild handle, or persist an explicit exact graph failure handle.
`accepted_unverified` is forbidden. After every canonical guard releases,
`ensure_started(handle)` SHALL start or join that registered exact flight without
consuming bounded waiter capacity. `join(handle)` SHALL separately admit a
bounded waiter and release its capacity in `finally` on every completion,
cancellation, and failure path.

Coordinator start failure, capacity failure, stopped/uncovered flight, lineage
conflict, stabilization exhaustion, and platform sharing refusal SHALL create
explicit durable handles with stable codes and recovery guidance. A
`canonically_committed` receipt can later join or recompute its exact handle
without canonical leaf replay.

#### Scenario: Handle registration is total before guard release
- **WHEN** an unusable graph prevents incremental acknowledgement
- **THEN** the exact checkpoint has a registered rebuild or failure handle before canonical authority releases
- **AND** the canonical request does not wait for graph work while holding authority

#### Scenario: Join capacity cannot leak
- **WHEN** a bounded join raises, times out, or is cancelled
- **THEN** its waiter slot is released
- **AND** a later valid join can be admitted

### Requirement: Incremental and full graph acknowledgement are exact

Incremental refresh SHALL run only when the durable checkpoint still equals the
receipt binding, the current graph acknowledgement is its exact immediate
predecessor, and all existing freshness, recall, and topology proofs pass. Rows,
availability markers, and all exact acknowledgement metadata SHALL commit in
one SQLite transaction. Failed proof SHALL roll back and register full work;
it SHALL not build under canonical authority.

Each full pass SHALL build a unique private temporary sidecar from a coherent
epoch and real full-vault freshness snapshot. Privately it SHALL commit exact
acknowledgement/checkpoint metadata, truncate WAL, run full integrity and direct
source/membership/topology proof, warm the live `RecallFreshnessCheckpoint`,
and capture recall-policy version plus bounded no-follow `_access.yaml`
content snapshot/fingerprint. It SHALL close the temp and emit an immutable
ticket binding exact graph publication epoch, direct projection identity, the
warmed checkpoint, policy/access snapshot, no-external-pending epoch, and exact
closed temp-file/meta identity. Under canonical authority it SHALL perform only
two O(1)/bounded ticket checks around the publication hook and atomic
replacement; no vault/sidecar walk, cache warm, WAL, integrity, or projection
proof is permitted. Any cold/dirty cache, epoch/access mismatch, observed
external Markdown/access edit, hook failure, or ticket mismatch discards the
ticket and retries via reconcile. Missed/unobserved direct edits converge by
watcher-periodic reconcile; arbitrary-editor linearizability requires an
out-of-scope broker/journal.

#### Scenario: Original-index edit races final publication
- **WHEN** the original index changes at either real freshness seam during final publication
- **THEN** the temporary pass is not published as current
- **AND** the exact handle retries or resolves to its durable failure outcome

### Requirement: Private rebuilds are cooperative and publication is non-partial

Within one configured trusted runtime root, at most one POSIX full builder per
canonical vault identity SHALL run cooperatively. That root SHALL be
operator-owned and neither group- nor world-writable; the persistent
descriptor-validated no-follow lock file SHALL sit directly beneath it, have
restrictive permissions, and never be unlinked. Hostile same-user replacement
of the trusted root is outside this cooperative contract. The same lock
serializes reserved-temp cleanup, which SHALL target only well-formed temps for
the exact vault identity and never a registered active flight, live sidecar, or
lock file.

Windows SHALL retain stronger no-delete-share locking/replacement behaviour. A
sharing or rename refusal SHALL preserve the old live sidecar and temporary
output, create an explicit graph failure handle, and SHALL NOT use
delete-and-replace. Readers SHALL see only an old usable sidecar, a complete
replacement, or unavailable—never a temporary or partial sidecar.

#### Scenario: Reader blocks Windows replacement safely
- **WHEN** a Windows reader keeps the live sidecar open without delete sharing
- **THEN** replacement is refused without changing the live sidecar
- **AND** after the reader closes, a new exact recovery attempt may publish

### Requirement: Lifecycle transitions and recovery preserve canonical truth

Graph-relevant file and recursive-directory deletion SHALL use the existing
lifecycle/trash transition under canonical authority: tombstone, floor, rename,
exact null/full checkpoint, and existing deletion commit point. A caught
checkpoint/later-batch failure SHALL reverse and fsync the rename and restore
the prior epoch before reporting failure. A crash after rename SHALL leave
lifecycle/floor evidence, force a higher full recovery generation, and neither
restore nor duplicate the deletion. Restore obeys the same ordering. Recovery
and rollback do not create a mutation receipt unless they are the originally
claimed caller leaf.

Readiness and reconcile SHALL report current only after independent exact epoch
parity and graph availability proof. Their repairs SHALL register an exact
rebuild/failure handle before authority releases.

#### Scenario: Caught recursive deletion rolls back
- **WHEN** a recursive trash transition fails after rename but before its checkpoint protocol completes
- **THEN** source placement and epoch are restored and fsynced
- **AND** no receipt or committed terminal is created

### Requirement: Installed-product proof covers the full contract

The installed wheel SHALL prove a disposable Records-compatible mutation through
the real MCP surface, including strict epoch creation, receipt/lifecycle,
derived graph recovery, and exact terminal/retry behaviour. Native Windows
verification SHALL exercise the installed-wheel service path, cross-process
rebuild claim, and open-reader replacement refusal. A quiesced 500/2,000-page
reproduction SHALL record p95 canonical boundary hold separately from request
latency and show that the hold does not scale with vault size.

#### Scenario: Disposable live MCP mutation crosses derived recovery
- **WHEN** a disposable installed service receives a real MCP mutation while its graph needs recovery
- **THEN** the canonical mutation commits once, graph work occurs off-boundary, and exact retry/inspection reports the required receipt and graph outcome
