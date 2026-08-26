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

#### Scenario: SQLite token-only churn does not veto a current replacement

- **WHEN** the live SQLite main, WAL, or SHM token changes during a detached
  repair
- **AND** source/projection checkpoints, policy, and semantic identity still
  match the replacement proof
- **THEN** token-only churn SHALL NOT decline publication

#### Scenario: Unprovable catch-up fails closed

- **WHEN** the required delta is incomplete or oversized
- **OR** source, policy, projection identity, or semantic identity cannot be
  proven current
- **THEN** the system preserves the live catalogue
- **AND** leaves the repair request pending for a later bounded flight

#### Scenario: Repair telemetry preserves vault privacy

- **WHEN** a detached repair advances, publishes, or declines
- **THEN** telemetry reports a bounded phase, duration, and stable result reason
- **AND** contains no vault path, note name, or note content
