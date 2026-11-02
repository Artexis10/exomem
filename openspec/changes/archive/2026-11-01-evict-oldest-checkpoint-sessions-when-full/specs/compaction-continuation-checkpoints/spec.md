## ADDED Requirements

### Requirement: A full state root evicts its oldest sessions

The state root SHALL hold a bounded number of entries. When a new session needs room that the bound does not leave, the hook SHALL first forget listed entries that no longer exist, and SHALL then prune the sessions whose state changed least recently, through the same tombstone path as an expired session, until the new session fits. Eviction SHALL lift only the age checks: the session SHALL still be a recognized, authorized session of this state root whose lock is free, and the current session SHALL NOT be evicted. If no session can be evicted, the new session's write SHALL fail without publishing partial state. Every `PreCompact` and client-supported `SessionEnd` SHALL prune before it writes, so a failed write does not stop pruning.

#### Scenario: A busy home fills the state root inside the retention window
- **WHEN** a new session writes its first checkpoint and the state root is full of sessions younger than the retention
- **THEN** the least recently changed session is pruned and the new checkpoint is written
- **AND** the other sessions remain

#### Scenario: Listed entries no longer exist
- **WHEN** the state root's listing is full of entries whose directories are gone
- **THEN** those entries are forgotten and the new checkpoint is written

#### Scenario: Nothing can be evicted
- **WHEN** the state root is full and no listed entry is a recognized, unlocked session other than the current one
- **THEN** the new session's write fails and no partial state is published

#### Scenario: A write fails
- **WHEN** a checkpoint write fails while expired sessions exist
- **THEN** the same hook run still prunes expired sessions
