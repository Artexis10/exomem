## ADDED Requirements

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
