## ADDED Requirements

### Requirement: Managed Lexical Repair Converges Under Live Traffic

While managed retrieval is unavailable, the system SHALL allow a detached full
lexical repair to publish under ordinary writer and watcher traffic without
weakening authoritative source, projection, policy, or semantic-identity
validation.

#### Scenario: Concurrent live write is rebased before publication

- **WHEN** a watcher generation changes or deletes Markdown paths while a
  detached full repair is building
- **AND** the complete bounded delta from the repair checkpoints to the current
  live checkpoints is retained
- **THEN** the system applies that delta to the completed replacement under the
  publication barrier
- **AND** publishes only after the replacement proves the current checkpoints
- **AND** managed retrieval can become ready without a process restart

#### Scenario: Large batch landing during publication wait catches up off-barrier

- **WHEN** a foreground watcher batch holds the publication barrier while a
  completed detached repair waits
- **AND** the retained final suffix is complete but exceeds the barrier replay cap
- **THEN** the system preserves the completed replacement and releases the barrier
- **AND** replays that suffix off-barrier without the foreground cap
- **AND** repeats the independent source proof before retrying publication
- **AND** limits catch-up retries so sustained live churn cannot monopolize repair

#### Scenario: Published handoff does not repeat a current full scan

- **WHEN** a successfully published and promoted repair leaves one generation
  request pending at its bounded idle handoff
- **AND** the next repair flight proves the persisted catalogue already covers
  the current live projection
- **THEN** the system acknowledges that handoff without another full-vault scan
- **AND** a stale proof or a repair request arriving during the proof still
  follows the normal full-repair path

#### Scenario: Safety-net reconcile proof survives restart

- **WHEN** the periodic safety-net walk discovers a filesystem event the live
  watcher missed
- **AND** it holds complete before and after recall maps under one unchanged
  policy identity
- **THEN** the system retains their exact changed/deleted set as a bridgeable
  recall delta carrying explicit reconcile provenance, never as a trusted
  watcher transition
- **AND** the lexical replay persists the resulting current checkpoint only
  after an independent off-barrier source proof matches that exact checkpoint
- **AND** a fresh process admits the current catalogue without a full rebuild

#### Scenario: Mixed reconcile walk cannot bless a stale catalogue

- **WHEN** the off-lock safety-net walk mixes pre- and post-change
  observations because a source path changed after the walk observed it
- **AND** the lexical replay applies the reconcile delta's changed/deleted set
- **THEN** the independent source proof fails to match the reconcile-derived
  checkpoint
- **AND** the system refuses to persist that scope's checkpoint and preserves
  the conservative repair path

#### Scenario: Source proof never holds the publication barrier

- **WHEN** a watcher batch must prove a reconcile-tainted checkpoint against
  the complete current source
- **THEN** the O(vault) proof walk executes before the publication barrier is
  acquired, with no locks held
- **AND** validation under the barrier is an O(1) exact-checkpoint comparison
- **AND** request and readiness paths refuse a reconcile-tainted delta without
  ever walking the vault

#### Scenario: Invalidated source proof refuses only the affected scope and converges

- **WHEN** an observed event, reconcile, or policy change lands between the
  off-barrier source proof and the publication barrier
- **THEN** the superseded proof fails closed and only the affected scope's
  checkpoint is refused
- **AND** sibling scopes with exact observed witnesses still persist
- **AND** the batch's rows still apply, and a later batch covering the current
  delta re-proves and persists the checkpoint without a full rebuild
- **AND** proof outcomes are counted in stable, content-free telemetry

#### Scenario: SQLite token-only churn does not veto a current replacement

- **WHEN** the live SQLite main, WAL, or SHM token changes during a detached
  repair
- **AND** source/projection checkpoints, policy, and semantic identity still
  match the replacement proof
- **THEN** token-only churn SHALL NOT decline publication

#### Scenario: Unprovable catch-up fails closed

- **WHEN** the required delta is incomplete
- **OR** the final suffix remains oversized after the bounded catch-up retries
- **OR** source, policy, projection identity, or semantic identity cannot be
  proven current
- **THEN** the system preserves the live catalogue
- **AND** leaves the repair request pending for a later bounded flight

#### Scenario: Repair telemetry preserves vault privacy

- **WHEN** a detached repair advances, publishes, or declines
- **THEN** telemetry reports a bounded phase, duration, and stable result reason
- **AND** contains no vault path, note name, or note content
