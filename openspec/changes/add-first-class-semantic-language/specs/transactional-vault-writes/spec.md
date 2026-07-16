## MODIFIED Requirements

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
destination replacement.

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
directory censuses. Detected or ambiguous drift SHALL fail closed and SHALL
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
result. Portable filesystems expose neither a conditional identity
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
