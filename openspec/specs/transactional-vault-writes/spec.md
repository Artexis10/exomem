# transactional-vault-writes Specification

## Purpose
Make multi-file vault writes behave transactionally: batch Markdown writes are all-or-nothing, and a move and its inbound wikilink rewrites roll back together. Graph-relevant writes keep fail-closed ordering unless a media caller explicitly requests deferred graph completion.

## Requirements

### Requirement: Multi-File Markdown Batches Roll Back

The batch write primitive SHALL provide all-or-nothing observable filesystem
state for ordinary caught staging or replacement failures under cooperating
Exomem writers. If commit fails after one or more destinations changed, it
SHALL restore every pre-existing destination's exact captured bytes and
supported metadata and SHALL remove every destination newly created by the
failed batch before returning the original failure. A detected inability to
restore one destination SHALL produce an explicit incomplete-rollback outcome,
SHALL NOT be reported as success, and SHALL NOT prevent guarded restoration of
the remaining destinations.

For every distinct destination parent, the primitive SHALL exclusively create
a random private transaction workspace in a reserved namespace. It SHALL bind
the workspace and every stage to the exact descriptor/handle and filesystem
identity created by the batch, use owner-only workspace permissions where the
platform supports them, use descriptor-relative traversal where available,
and reject symlink or reparse traversal. A pathname fallback SHALL revalidate
the bound workspace and leaf identity immediately before use. Planned bytes
SHALL be written and verified through the owned stage descriptor before any
destination replacement. The bytes copied from a stage into a destination
SHALL be hashed while they are copied, and the destination SHALL be installed
only when that digest matches the planned digest. A mismatch SHALL leave no
destination installed for that stage.

Before the first destination replacement, the primitive SHALL capture every
pre-existing destination in memory as exact bytes plus permission bits,
nanosecond access and modification timestamps, and the complete set of
descriptor-enumerable/restorable extended attributes where those APIs are
supported. Ownership, ACLs not represented as those extended attributes,
birth time, and change time are outside this portable metadata contract. Any
non-unsupported capture error SHALL abort before the first replacement. An
existing destination SHALL be restored only from a fresh descriptor-owned
stage written from that in-memory snapshot; a newly created destination SHALL
be removed only while it still matches the exact batch-installed identity and
content at the last observable guard.

The primitive SHALL NOT create, search for, open, or classify any named
pathname as backup or rollback state. Pre-existing user files, including
`.bak` files, SHALL remain ordinary guarded census entries and SHALL never be
treated as rollback artifacts. Before every replacement, restore, or removal,
the primitive SHALL revalidate target identity/content guards, workspace
identity, stage identity/content, already installed finals, and applicable
directory censuses. A binary stage and an installed binary destination MAY be
revalidated by their exact file identity and a trusted platform change
witness (size, modification time, and change time) instead of rereading their
bytes; a platform or route without a trusted change witness SHALL keep the
content check. Each binary's bytes SHALL be hashed when they are copied into
the stage and when they are copied into the destination, and SHALL NOT be
reread for each further replacement where a trusted change witness exists. Detected or ambiguous drift SHALL fail closed and SHALL
NOT overwrite, move, or delete the changed path.

Rollback SHALL attempt every still-safe destination after any individual
restore failure. The primitive's outer error SHALL add one structured rollback
outcome with a stable code, committed/incomplete state, total affected count,
at most 16 vault-relative logical target paths, an omitted-target count, and
remediation. Internal diagnostic chaining MAY retain the original exception
object, but the outer message and every public command, REST, MCP, or CLI error
envelope SHALL NOT serialize or interpolate raw causes, absolute vault paths,
raw workspace/stage names, or raw low-level filesystem messages.

A clean successful commit or complete rollback SHALL leave each private
workspace empty and remove it with a non-recursive directory removal.
Unexpected entries, workspace drift, or unresolved installed-final drift SHALL
retain the private workspace for governed reconciliation and SHALL never
trigger recursive deletion. A batch SHALL report clean success only after
every owned workspace is proven empty and removed. If every destination
committed but safe workspace cleanup cannot complete, the primitive SHALL
preserve the committed destinations, run the existing post-commit
registration/index fan-out exactly once, and return a structured
`cleanup_incomplete` outcome with `committed=true`, the same bounded logical-
target summary, and reconciliation guidance. It SHALL NOT describe that state
as rollback failure or retry the committed replacements.

Abrupt private residue SHALL never be rollback or recovery authority; an exact
retry SHALL create fresh workspaces and stages from current guarded inputs.
Directory census and capacity checks SHALL classify a residue workspace as
valid for the current guard evaluation only when its name is
`.exomem-batch-` followed by exactly 32 lowercase hexadecimal characters, it is
a real non-symlink/non-reparse directory with no group/other permission bits
where POSIX mode bits apply, and every observed child is a real
non-symlink/non-reparse regular file named `stage-<decimal>.tmp` or
`restore-<decimal>.tmp`. After child validation and workspace identity and
metadata checks, classification SHALL end with a fresh bounded child census
that revalidates the observed names, identities, and types against the
validated set; workspace timestamp comparison alone SHALL NOT replace that
final census. Each census SHALL examine at most 4,096 children, and
classification SHALL examine at most 64 residue workspaces per parent.
Overflow or unsafe or drifting state detected by these bounded observations
SHALL fail closed with stable code `BATCH_RESIDUE_LIMIT` or
`BATCH_RESIDUE_UNSAFE`. Valid classified residue SHALL neither become a user
destination nor block an otherwise exact retry. Classification is a bounded
observational guarantee, not a frozen namespace snapshot.

The ordinary caught-failure guarantee excludes only mutation by a process
running as the vault owner in the unavoidable interval after the last check
relevant to a pathname property and before that observation is consumed. This
includes deliberate substitution of an exact batch-controlled source-stage,
workspace, or destination pathname component before the corresponding kernel
namespace instruction, and mutation of a residue workspace or child after its
last relevant check before the directory guard consumes the classification
result. It also includes a same-size rewrite of a binary stage or an installed
binary destination within one filesystem timestamp tick of its last change
witness, and a same-size rewrite of a private publication temporary, or of the
destination it was just installed as, that is made after the verified copy and
before the installed witness is read and leaves the size and modification time
unchanged. Portable filesystems expose neither a conditional identity
precondition for every pathname component nor a portable frozen directory-
census primitive. This narrow exclusion does not weaken rollback for detected
drift, ordinary concurrency detected by the applicable checks, cooperating
writers, staging/replacement exceptions, or restore failures, and the
primitive does not claim cross-file all-or-none power-loss atomicity.

#### Scenario: Second replacement fails

- **WHEN** a two-file batch replaces the first destination and fails while replacing the second under cooperating writers
- **THEN** both destinations have their exact pre-batch bytes and supported metadata after rollback
- **AND** the operation reports the original failure rather than success

#### Scenario: Failed batch included a new file

- **WHEN** a failed batch already created a destination that did not exist before the batch and the exact installed identity remains current
- **THEN** rollback removes that new destination and reports the original failure

#### Scenario: Metadata capture failure prevents mutation

- **WHEN** supported metadata on any existing destination cannot be captured for a reason other than an unsupported platform capability
- **THEN** the batch fails before replacing any destination

#### Scenario: Supported metadata is restored

- **WHEN** a handled multi-file failure occurs after an existing destination with permission bits, nanosecond timestamps, or descriptor-restorable extended attributes was replaced
- **THEN** rollback restores its exact prior bytes and every supported captured metadata value

#### Scenario: One restore failure does not abandon the rest

- **WHEN** rollback cannot safely restore one changed destination but other changed destinations remain guard-valid
- **THEN** it attempts the remaining restores and returns one bounded incomplete-rollback outcome chained to the original commit failure

#### Scenario: Rollback uses no named backups

- **WHEN** a batch succeeds, fails during staging, fails during replacement, or rolls back beside a pre-existing user `.bak` file
- **THEN** no named pathname is created, searched, opened, or classified as rollback state and the user `.bak` file remains an unchanged ordinary census entry

#### Scenario: Detected rollback drift retains private residue safely

- **WHEN** a handled failure encounters a changed final, workspace, stage, census, or supported metadata state during rollback
- **THEN** it does not overwrite or delete the changed path, retains only the necessary private residue, and returns bounded vault-relative reconciliation guidance

#### Scenario: Abrupt residue is not recovery authority

- **WHEN** a prior interrupted batch left a private workspace and an exact batch is retried
- **THEN** the retry constructs fresh stages from current guarded inputs and neither trusts nor reopens the residue as rollback state

#### Scenario: Complete outcomes leave no workspace residue

- **WHEN** a batch succeeds or every changed destination is completely rolled back
- **THEN** its private workspaces are empty and removed without recursive deletion

#### Scenario: Post-commit cleanup drift is reported as committed residue

- **WHEN** all destinations commit and a workspace is substituted or gains an unexpected entry before cleanup
- **THEN** committed destinations remain unchanged, post-commit fan-out runs once, and the operation does not report clean success
- **AND** its bounded `cleanup_incomplete` outcome distinguishes committed cleanup residue from incomplete rollback

#### Scenario: Outer errors sanitize raw diagnostic causes

- **WHEN** an internal filesystem exception contains an absolute path, raw workspace name, or low-level message and the primitive returns a rollback or cleanup outcome
- **THEN** internal exception chaining may retain the original object while the outer message and serialized public envelope contain only the stable code, bounded logical-target summary, state, and remediation

#### Scenario: Post-verification same-owner mutation is the sole portability exclusion

- **WHEN** a process running as the vault owner changes a relevant pathname component after its last verified check and before that observation is consumed, including final-instruction substitution or residue mutation after its final bounded census
- **THEN** the primitive does not claim a portable conditional-identity or frozen-directory-census guarantee for that interval
- **AND** detected-drift, applicable ordinary-concurrency, and cooperating-writer guarantees remain unchanged

#### Scenario: Tampered stage is never published

- **WHEN** a staged binary changes after its last witness check and before its bytes are copied into the destination
- **THEN** the copy's digest does not match the planned digest and no destination is installed for that stage
- **AND** every destination the batch already changed is rolled back, the changed stage is retained as private residue rather than deleted, and the operation reports a cleanup-incomplete outcome chained to the content failure rather than an incomplete rollback

#### Scenario: Binary bytes are hashed at their two handoff points

- **WHEN** a batch of any number of writes installs a binary on a platform with a trusted change witness
- **THEN** the binary's bytes are hashed once while copied into the stage and once while copied into the destination
- **AND** later revalidation of that stage and that installed destination compares identity and change witness without rereading the bytes

#### Scenario: Untrusted change witness keeps content checks

- **WHEN** the platform does not expose a trusted change witness for an installed binary destination
- **THEN** that destination is revalidated by its exact content before each later replacement, restore, or removal

#### Scenario: Rollback removal of a large binary streams its content check

- **WHEN** rollback removes a binary destination that the failed batch installed
- **THEN** it verifies the exact installed content through a bounded buffer and never holds the whole file in memory

### Requirement: Moves Roll Back Link Rewrites

`move_file` SHALL treat the file relocation and its inbound wikilink rewrites as one reversible operation. A failure rewriting links MUST restore the original path and original inbound-link contents, and a failed relocation MUST leave inbound links untouched.

#### Scenario: Link rewrite fails after rename

- **WHEN** the target file is renamed and the inbound-link batch then fails
- **THEN** the target is restored at its original path
- **AND** all inbound-link files retain their original content
- **AND** the destination path does not remain as a duplicate

#### Scenario: Successful move remains single-copy

- **WHEN** a move and all inbound-link rewrites succeed
- **THEN** only the destination path exists
- **AND** every selected inbound wikilink targets the destination

### Requirement: Deferred Graph Completion Omits The Shared Checkpoint

When a media caller explicitly requests deferred graph completion, `batch_atomic_write` SHALL compute one exact graph checkpoint, stage its generation floor before caller-supplied canonical writes, and SHALL NOT stage, replace, or roll back the shared graph checkpoint. The option SHALL require immediate post-commit fanout to be disabled and SHALL return the exact deferred checkpoint and its admitted predecessor to the owning boundary. All existing write guards SHALL still apply to the floor and caller writes.

Every caller that does not explicitly request deferred graph completion SHALL retain existing graph epoch behavior, including callers that set `post_commit_fanout=False` for graph-internal or recovery operations.

#### Scenario: Windows graph checkpoint is held open

- **WHEN** a valid media sidecar is committed with deferred graph completion while another Windows handle denies deletion of `.graph-sync.json`
- **THEN** the graph floor and canonical sidecar commit in that order
- **AND** the batch does not attempt to replace the shared checkpoint
- **AND** no rollback workspace is retained because of that handle

#### Scenario: Deferred batch guard fails

- **WHEN** a floor or caller write fails a required path or identity guard
- **THEN** the existing fail-closed refusal and rollback behavior applies to every staged write

#### Scenario: Crash remains visibly stale

- **WHEN** the process stops after floor and canonical commit but before checkpoint publication
- **THEN** graph status is recovery-required or unavailable for the installed generation
- **AND** the old graph is not reported current

#### Scenario: Exact checkpoint crosses the guard

- **WHEN** deferred graph completion returns and post-guard fanout begins
- **THEN** the canonical coordinator verifies that the floor and predecessor still match the admitted token
- **AND** the checkpoint paired with the installed floor is published before graph/index fanout
- **AND** success does not substitute an old or newly manufactured full-scope checkpoint

#### Scenario: Newer writer supersedes deferred token

- **WHEN** another canonical writer advances the epoch after a deferred sidecar commit and before its checkpoint postlude
- **THEN** the stale deferred token does not overwrite or regress the newer floor or checkpoint
- **AND** it does not clear current refresh work or claim its fanout complete

### Requirement: Ordinary Graph Transactions Remain Fail-Closed

Unless deferred graph completion is explicitly requested, graph-relevant writes SHALL retain floor → caller writes → checkpoint ordering independent of `post_commit_fanout`. A failure after replacement SHALL retain existing rollback, residue, and failure reporting. Exomem SHALL NOT automatically retry a failed or ambiguously committed batch.

#### Scenario: Ordinary checkpoint replacement fails

- **WHEN** an ordinary graph transaction fails while replacing its checkpoint after an earlier destination changed
- **THEN** the existing rollback protocol runs
- **AND** incomplete rollback remains explicit
- **AND** the batch is not replayed blindly

### Requirement: Canonical page identity hashes stored bytes

A hash that identifies a canonical page, guards its replacement, or proves derived custody SHALL equal SHA-256 of its exact stored bytes. UTF-8 decoding and Markdown parsing MUST NOT normalize bytes before computing that identity. Deliberate semantic fingerprints SHALL remain distinct from canonical page identity.

#### Scenario: A page contains CRLF newlines

- **WHEN** a reader, write guard, or derived consumer identifies the same stored CRLF page
- **THEN** each identity equals SHA-256 of those stored bytes
- **AND** an unchanged page does not fail a guard because another reader normalized its newlines

#### Scenario: A page changes after its guarded read

- **WHEN** another writer changes the bytes after a guarded read
- **THEN** the existing drift checks refuse the stale replacement or publication

#### Scenario: A page starts with a UTF-8 byte order mark

- **WHEN** a canonical page has one UTF-8 byte order mark before its Markdown frontmatter fence
- **THEN** readers parse its metadata through the shared fence definition and hash its exact stored bytes
- **AND** incomplete frontmatter, malformed YAML and duplicate identities retain their existing refusal behavior
- **AND** existing reference and semantic parser versions invalidate derived rows built under the earlier parser

### Requirement: Identity backfill preserves existing document bytes

Adding a missing page identity SHALL preserve the document's existing newline representation and content outside the inserted field. Existing malformed or duplicate identity checks SHALL remain unchanged.

#### Scenario: Backfill adds an identity to a CRLF page

- **WHEN** backfill adds an identity to valid frontmatter in a CRLF page
- **THEN** existing document lines retain their newline bytes and the resulting identity hashes the exact installed bytes

### Requirement: Derived identity repair never rewrites canonical pages

Existing recovery MAY refresh derived hashes from current guarded page bytes. A hash mismatch MUST NOT authorize newline conversion or another canonical rewrite. Unprovable visibility SHALL retain its existing explicit unavailable state.

#### Scenario: A legacy derived row has a normalized-text hash

- **WHEN** normal recovery refreshes that row from an unchanged CRLF page
- **THEN** the derived identity becomes byte-exact and the canonical page remains unchanged
