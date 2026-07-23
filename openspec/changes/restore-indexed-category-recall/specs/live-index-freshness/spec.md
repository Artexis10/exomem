## ADDED Requirements

### Requirement: Freshness Registry Exposes Atomic Consumer Deltas

Each live scope registry SHALL have a process-instance ID and monotonic generation. A consumer checkpoint SHALL contain `{instance_id, generation, triple}`. An atomic delta request SHALL return `{from, to, complete, changed, deleted}` for one captured target generation, where `changed` and `deleted` are duplicate-free, mutually disjoint target-state path sets and `to` contains the exact target checkpoint. A path present at `to` SHALL appear only in `changed`; a path absent at `to` SHALL appear only in `deleted`, regardless of intermediate events. Multiple consumers MUST read deltas non-destructively.

#### Scenario: Edit and rename have exact representations

- **WHEN** one file is edited and another is renamed after a consumer checkpoint
- **THEN** a complete delta lists the edit and rename destination in `changed` and the rename source in `deleted`
- **AND** its `to` checkpoint identifies the exact snapshot containing those events

#### Scenario: Edit then delete coalesces to deletion

- **WHEN** one path is edited and then deleted between `from` and `to`
- **THEN** it appears only in `deleted`
- **AND** apply order cannot resurrect it

#### Scenario: Delete then recreate coalesces to change

- **WHEN** one path is deleted and recreated before `to`
- **THEN** it appears only in `changed`
- **AND** apply order cannot remove the recreated target state

### Requirement: Unknown Delta Never Returns A Partial Suffix

Process restart, reconciliation mismatch, retained-history overflow, a foreign instance ID, or a checkpoint older than retained history SHALL return `complete=false`. An incomplete response MUST NOT expose a retained suffix as if it were the full delta. A later event arriving after a captured `to` generation MUST remain discoverable from that `to` checkpoint.

#### Scenario: Overflow is explicitly incomplete

- **WHEN** event history overflows before a consumer requests its delta
- **THEN** the registry reports `complete=false`
- **AND** no consumer can advance its authoritative checkpoint from the incomplete response

#### Scenario: Concurrent event remains for the next delta

- **WHEN** an event arrives after `delta_since` captures its target generation but before the consumer commits repair
- **THEN** the current delta's `to` checkpoint remains unchanged
- **AND** requesting from that checkpoint returns the later event

### Requirement: Delta Application Advances Checkpoint Atomically

A sidecar consumer applying a complete delta SHALL commit all changed-path upserts, deleted-path removals, and the exact `to` checkpoint in one transaction. On rollback, neither rows nor checkpoint may advance.

#### Scenario: Failed patch cannot bless stale rows

- **WHEN** any path update fails while applying a complete delta
- **THEN** the transaction rolls back every row change and retains the prior checkpoint
- **AND** the next request still observes the delta as unapplied
