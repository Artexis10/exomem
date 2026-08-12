## Context

An unusable graph sidecar currently makes the canonical mutation boundary cover
the full rebuild. That makes a vault-sized derived operation block unrelated
canonical writes. A previous redesign released the boundary, but made
`graph_pending` both an idempotency state and a graph-flight owner. Review found
that this leaves terminal, retry, crash, and newer-checkpoint behaviour
ambiguous. This design separates them.

The graph is derived. Canonical Markdown and the exact committed checkpoint are
truth. The graph must still be available for the required checkpoint before a
successful graph-available terminal is returned, but its work must be
joinable/recomputable independently of the original mutation attempt.

## Goals / Non-Goals

**Goals:**

- Commit canonical bytes exactly once or return an honest `outcome_unknown`
  terminal classification.
- Make a committed mutation independently identifiable by an exact receipt.
- Release all canonical authority before any vault-sized build or join.
- Require an acknowledgement, exact registered work handle, or durable failure
  handle for every checkpoint that leaves writer authority.
- Preserve public checkpoint v1 bytes/digest vectors and strict availability
  parity.
- Recover graph health without changing user-authored bytes or reusing an epoch.

**Non-Goals:**

- Make graph availability best-effort or silently asynchronous.
- Infer an unreceipted canonical outcome from filesystem similarity.
- Change user-authored schemas, query APIs, or the public checkpoint v1 shape.
- Claim mutual exclusion across hostile replacement of a same-user trusted
  runtime root; that root is an operator-controlled trust boundary.

## Decisions

### Canonical lifecycle and claim receipts

The retry row is a non-owned canonical lifecycle record with exactly four
states: `reserved`, `executing`, `canonically_committed`, and `completed`.
`graph_pending` is retired. A bounded execution **attempt** may claim a
`reserved` row and move it to `executing`; the attempt identity and its lease
are attempt-scoped coordination data, not ownership carried by a later state.
A dead `reserved` row is reclaimable. A dead `executing` row is not: absent
valid authenticated evidence, its only recovery classification is
`outcome_unknown`, because the leaf may have crossed the vault cut.

`outcome_unknown` is not a fifth canonical lifecycle state and does not imply a
canonical commit. It is a fail-closed terminal classification for the
unprovable multi-store cut. The retry store retains that immutable
classification in a `completed` row only as a terminal storage envelope; this
keeps the state vocabulary closed without asserting that the canonical mutation
completed. Exact retry receives readback guidance rather than permission to
execute the leaf again.

Each claim receives a fresh lowercase 24-hex `commit_token` and a fresh private
32-byte receipt secret. `GraphCommitReceipt` v2 is one atomically installed,
hidden, portable, content-free receipt retained at least as long as its retry
row. It binds all of:

- namespaced idempotency-key digest;
- command and normalized payload digest;
- claim/attempt identity and `commit_token`;
- bounded canonical terminal projection; and
- exact graph checkpoint generation and digest.

Receipt eligibility is not circular. “The complete ordinary protocol
succeeded” means, before signing: the claimed leaf has returned its candidate
terminal; the one guarded vault batch has successfully applied its selected
floor, caller canonical replacements, and exact checkpoint; and every bounded
canonical fanout precondition needed to select and validate that checkpoint's
incremental-or-rebuild route has returned successfully. It excludes receipt
installation/authentication, the local lifecycle CAS, durable derived-handle
registration, `ensure_started`, every graph join, and completed-terminal
persistence. Any failure before that point installs no receipt and follows the
ordinary batch rollback/error path.

The terminal projection is a closed content-free object with only these
permitted fields, each omitted when unavailable:
`_terminal`, `version`, `ok`, `state`, `status`, `committed`, `mutated`,
`terminal`, `request_id`, `receipt_id`, `operation_id`, `warnings_count`, and
`result_sha256`. Scalar identifiers have their existing bounded formats;
`result_sha256` is lowercase SHA-256 of an otherwise non-retained result
summary. No other top-level or nested projection fields are permitted. In
particular, `path`, `paths`, `affected_paths`, all vault-relative/absolute
paths, file names, Markdown, metadata values, source text, and arbitrary leaf
result/content are excluded. Graph outcome is attached only after receipt
persistence and is not part of this projection.

Receipt v2 itself is closed. Its exact outer fields are `version`,
`idempotency_key_digest`, `command_digest`, `attempt_id`, `commit_token`,
`terminal_projection`, `terminal_projection_sha256`, `checkpoint_generation`,
`checkpoint_sha256`, `canonical_disposition`, and `receipt_hmac_sha256`;
`version` is literal integer `2`, `canonical_disposition` is exactly `success`
or `committed_failure`, the two checkpoint fields are both null or a positive
integer/lowercase SHA-256 pair, and every other digest is lowercase SHA-256.
`canonical_disposition` is covered by the HMAC; it does not alter the closed
terminal-projection allowlist. Its HMAC input is
the UTF-8 bytes `exomem-graph-commit-receipt-auth:v2\0` concatenated with the
canonical UTF-8 JSON of every v2 field except `receipt_hmac_sha256`. Canonical
JSON uses no insignificant whitespace, lexicographically sorted object keys,
and unescaped non-ASCII Unicode; duplicate keys, surplus keys, missing keys,
wrong types, noncanonical values, and a mismatched terminal-projection digest
are rejected before authentication. Verification recomputes the HMAC-SHA256 and
uses a constant-time digest comparison. This is a complete definition, not a
reference to the receipt parser or writer.

The receipt carries an HMAC-SHA256 over its canonical v2 bytes using that
secret. The secret exists only in the matching owner-only local idempotency
runtime row, never in a synced vault file, terminal, log, or diagnostic. On
POSIX, the runtime directory is owner-only (`0700`) and its database, WAL/SHM
files, and receipt-secret material are owner read/write only (`0600` or
stricter). On Windows, the local SQLite BLOB is exactly
`EXID | version=1 | provider=1(DPAPI_CURRENT_USER) | uint32be ciphertext_length | ciphertext`;
both total BLOB and ciphertext are capped at 4096 bytes and declared length
must exactly consume the remaining bytes. Truncation, trailing bytes, and
unknown version/provider are rejected. DPAPI `CurrentUser` uses UTF-8 entropy
`exomem-graph-commit-receipt-dpapi:v1\0<attempt_id>\0<commit_token>`; the clear
secret is never serialized. The protected inheritable runtime DACL permits only
the current service identity, `LocalSystem`, and `Administrators`. Before
opening SQLite or unpickling any runtime row, the service rejects a
reparse-point runtime path or an existing unsafe DACL. The database and its
WAL/SHM therefore contain only the DPAPI ciphertext. An account/profile change,
copied envelope, failed DPAPI unprotect, or raw legacy secret row is
`outcome_unknown`, never authority to heal or replay.
The private runtime root is a distinct trust boundary from the synced vault.

There is no mutable `commit_point` field and no second marker write. The one
receipt is authoritative only after the defined pre-signing ordinary protocol
succeeded. It is installed atomically before the local CAS from the matching
`executing` row to `canonically_committed`. A post-install local CAS failure may
be healed only by the exact receipt plus its matching trusted `executing` row,
attempt/token binding, and retained private secret. A copied, valid-looking
receipt without that row and secret is advisory and cannot suppress a mutation.
No recovery path scans receipt directories: it reads only the exact receipt
address named by the trusted row, with bounded artifact size and bounded parsing
work. No state owns subsequent graph health. The exact graph coordinator can be
joined or recreated from a committed row and its exact receipt/checkpoint by a
later attempt, retry, readiness, or reconcile process. `completed` stores the
returned graph-available or committed-derived-failure terminal and remains
replayable byte-for-byte.

There is no transaction shared by the vault and retry store. The crash cuts are
closed: before the leaf, a dead `reserved` claim may be reclaimed; after the
leaf can change caller files, floor, or checkpoint but before receipt v2 atomic
installation, the claim is `outcome_unknown`; after receipt installation but
before local CAS, only the matching trusted executing row and secret may heal
that CAS; and a failure while cleaning a committed attempt never reconstructs
success from a receipt after trusted state is absent. No cut may replay the leaf,
fabricate a committed terminal, or infer success from vault similarity. Exact
retry returns stable readback guidance; epoch recovery may converge derived
graph health but cannot turn an unproven claim into a receipt.

An authenticated orphaned v2 receipt with `canonical_disposition: success` may
heal only its matching trusted executing CAS and then resumes its ordinary
derived graph path. An authenticated orphan with
`canonical_disposition: committed_failure` suppresses leaf replay: if its exact
retained local failure is present it is replayed, otherwise the row persists
`outcome_unknown` while graph recovery proceeds independently. Missing,
invalid, or fieldless v2 receipts never heal either disposition.

Synced vault receipt artifacts are untrusted/advisory even when their HMAC bytes
look valid, because the private secret is not synced. Cross-replica exactly-once
is explicitly deferred: without a shared pre-leaf CAS, independent replicas
cannot use a copied receipt to suppress one another's mutation. Legacy v1
boolean receipts are advisory only. Legacy `graph_pending` migration is local
retry-row migration only; a validated local row may become
`canonically_committed` and rejoin/recompute graph work, but vault artifacts
alone never create that row or authorize a leaf decision.

### Epoch publication and exact ordering

The public `<Knowledge Base>/.graph-sync.json` remains closed v1, including its
canonical bytes and existing digest vectors. The internal
`.graph-sync-floor.json` remains a closed `{version, generation,
floor_sha256}` object in its distinct digest domain. The checkpoint parser is
strict before normalization: its `paths` array contains at most 1,000 entries;
paths are unique, already NFC-normalized, safe canonical POSIX-relative paths
(no absolute path, backslash, empty, dot/dotdot, or NUL component); hashes are
null or lowercase 64-hex; duplicate
paths cannot conflict; `created_paths` is a unique subset of non-null paths;
and `full` has empty arrays. The parser accepts neither surplus fields nor
relaxed legacy aliases.

For an ordinary graph-relevant claimed mutation, the canonical protocol is:

1. under canonical authority select one generation above the maximum valid
   floor, checkpoint, and acknowledgement;
2. publish floor;
3. publish caller canonical replacements;
4. publish the exact checkpoint; and
5. after the vault batch succeeds, atomically install the exact authenticated
   receipt v2, then compare-and-swap its matching trusted executing row to
   `canonically_committed`.

Caught failure rolls back the whole vault batch while authority remains held;
the receipt is not installed. Graph-irrelevant batches advance neither floor
nor checkpoint and create no graph receipt. Setup, floor seeding, reconcile
repair, rollback, and cleanup explicitly run with `commit_point=False`: they
may repair epoch state but never prove a command mutation. This API flag is not
a receipt field or marker.

Exact legacy is all floor, checkpoint, and acknowledgement absent. A valid
checkpoint with absent floor and absent/exact acknowledgement is the only
pre-floor migration state and may atomically seed the floor. A valid floor with
missing, malformed, or older checkpoint recovers only with a higher full-scope
checkpoint. Every other malformed/missing-floor or contradictory-ack state
fails closed. Existing v1 acknowledgement metadata remains strict: availability
requires every currently written exact metadata key, including
`graph_sync_checkpoint`, and exact generation/digest parity. There is no
defaulting-away of missing metadata in this change.

### Derived handoff, coordinator, and terminal outcomes

Before canonical authority releases, an ordinary graph-relevant checkpoint must
have exactly one of these durable/provable fates:

1. incremental SQLite work atomically acknowledges that exact checkpoint;
2. a durable exact rebuild handle is registered; or
3. a durable explicit graph failure handle is attached to the receipt.

`accepted_unverified` is forbidden. Registration is the under-guard durable
handoff only; after all canonical guards release, `ensure_started(handle)`
atomically starts or joins its exact coordinator flight and does **not** consume
a waiter slot. `join(handle)` is the separate bounded-waiter operation and
releases its capacity in `finally`, including start, cancellation, and failure
paths. A coordinator capacity/start/stopped-flight/lineage/platform failure
becomes an explicit handle with stable code and recovery guidance; no waiter is
orphaned. A later exact retry or reconcile can join or recompute any
`canonically_committed` receipt's graph health without a leaf replay.

Incremental refresh is eligible only when the current checkpoint still equals
the committed receipt, the graph acknowledgement is its exact immediate
predecessor, all existing freshness/recall/topology proofs hold, and rows,
availability markers, and all acknowledgement metadata commit in one SQLite
transaction. Otherwise the leaf only records the exact rebuild handle; it does
not build or join while authority is held.

The triggering request may join off-boundary. It returns a normal committed
terminal only after exact availability; if its exact handle reaches a bounded
derived failure, it returns committed-with-graph-failure, the exact checkpoint,
and recovery guidance. This does not change the receipt's canonical proof. A
later successful recovery changes live graph health but does not rewrite the
original completed terminal.

### Single-flight private build and publication

For one configured trusted runtime root, POSIX rebuild single-flight is
cooperative: the root must be operator-owned and neither group- nor
world-writable, and the persistent per-vault lock file sits directly beneath it.
The regular file is opened and validated no-follow with restrictive permissions
and is never unlinked. The contract deliberately excludes a hostile same-user
replacement of that trusted root. The lock serializes builders and scoped temp
cleanup; it does not itself make reader-visible publication correct.

Windows retains stronger semantics: the lock and live-sidecar replacement use
no-delete-share handles. A sharing/rename refusal preserves the old live
sidecar and the temporary result, records an explicit graph failure handle, and
never falls back to delete-and-replace.

A builder acquires the rebuild lock outside canonical authority, creates a
unique reserved temp sidecar, snapshots coherent epoch plus real full-vault
freshness, and performs all scalable work privately. The private phase commits
exact acknowledgement/checkpoint metadata, truncates WAL, runs full integrity
and direct source/membership/topology proof, warms the live
`RecallFreshnessCheckpoint`, and captures the recall-policy version plus a
bounded no-follow `_access.yaml` content snapshot/fingerprint. It then closes
the temp and emits an immutable ticket binding exact graph publication epoch,
direct projection identity, warmed checkpoint, policy/access snapshot,
no-external-pending epoch, and exact closed temp-file/meta identity.

Under canonical authority, publication performs only two O(1)/bounded ticket
checks around the publication hook and then atomic replacement. It does not
walk the vault or sidecar, warm caches, or run WAL, integrity, or projection
proofs. A cold/dirty cache, changed epoch/access snapshot, observed external
Markdown/access edit, hook failure, or ticket mismatch releases authority and
causes reconcile/retry. Missed or unobserved direct edits are eventually
caught by watcher-periodic reconcile; arbitrary-editor linearizability requires
a broker/journal and is out of scope. Readers see only the old usable sidecar,
the fully replaced sidecar, or unavailable—never a temp/partial database.

Cleanup is scoped to a well-formed reserved temp for the exact vault identity;
it runs only while holding the rebuild lock, never removes the persistent lock
or live sidecar, and never sweeps a registered active flight. The same exact
handle protocol makes a newer checkpoint joinable while an older pass retries.

### Deletion, recovery, and rollback

File and recursive-directory deletion use the existing lifecycle/trash
transition under canonical authority: tombstone, floor, rename, exact null/full
checkpoint, then the existing deletion commit point. A caught checkpoint or
later batch failure reverses and fsyncs the rename and restores the epoch before
returning failure. A crash after rename leaves lifecycle/floor evidence and
requires a higher full recovery checkpoint; it neither restores nor duplicates
the deletion. Restore follows the same guarded ordering. These lifecycle and
recovery paths have `commit_point=False` unless they are the claimed caller
leaf itself, so repair can never manufacture a retry receipt.

Readiness/reconcile dynamically classify floor/checkpoint/ack and graph
availability. They report current only after independent exact epoch parity and
availability proof. A repair must produce an exact registered rebuild/failure
handle before releasing authority. No recovery changes user-authored bytes
except the original requested lifecycle operation.

## Risks / Trade-offs

- The multi-store cut remains observable as the fail-closed
  `outcome_unknown` terminal classification; hiding it would risk duplicate
  canonical writes.
- Receipts add bounded durable state, but make retry and graph recovery auditable
  without storing user content.
- Coordinated POSIX locking needs an operator-controlled runtime root; Windows
  may decline unsafe replacement rather than pretending it succeeded.
- Sustained writes can exhaust bounded stabilization; that is a committed
  derived failure with a recoverable handle, never a writer-bound deadlock.

## Migration Plan

The public checkpoint v1 representation is unchanged, including its canonical
bytes and digest vectors. Exact legacy remains valid until first graph-relevant
publication. Valid pre-floor checkpoint states seed the floor; all other
malformed migration states fail closed. Legacy v1 boolean receipts are advisory
only. Existing `graph_pending` **local retry rows** are read as
`canonically_committed`, retain their validated checkpoint binding, and cannot
run the leaf again; no synced artifact can synthesize that migration.

A rollback compatibility build retains strict v1 floor/checkpoint/ack parsing,
NFC path admission, advisory receipt reading, local `graph_pending` migration,
outcome-unknown behaviour, and recovery. It may disable new scheduling only
behind the explicit rollback gate; it must not reintroduce a writer-held rebuild
or relax acknowledgement parity.
