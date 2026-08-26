# transactional-vault-writes Specification

## Purpose
TBD - created by archiving change support-unicode-titles-and-vault-integrity. Update Purpose after archive.
## Requirements
### Requirement: Multi-File Markdown Batches Roll Back

The batch write primitive SHALL provide all-or-nothing observable filesystem state for ordinary caught staging or replacement failures. If commit fails after one or more destinations changed, the system MUST restore every pre-existing destination and remove every destination newly created by the failed batch before returning an error.

#### Scenario: Second replacement fails

- **WHEN** a two-file batch replaces the first destination and fails while replacing the second
- **THEN** both destinations have their exact pre-batch bytes after rollback
- **AND** the operation reports failure rather than success

#### Scenario: Failed batch included a new file

- **WHEN** a failed batch had already created a destination that did not exist before the batch
- **THEN** rollback removes that new destination

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
