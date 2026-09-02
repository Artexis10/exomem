## ADDED Requirements

### Requirement: Expensive Writer Fanout Has Exact Component Custody

Every expensive derived component omitted from the synchronous writer path
SHALL receive exact durable path-and-generation custody before the write is
acknowledged. The running server SHALL own a prompt, bounded, mode-aware drain;
restart and periodic reconciliation SHALL remain backstops. Completion SHALL
clear only the exact receipt revision whose generation was published. A covered
component deferral MUST NOT mint a redundant full-upsert receipt, while an
uncovered or unreadable receipt MUST fail closed into explicit reconciliation
demand.

#### Scenario: Several components defer one batch

- **WHEN** graph, embeddings, claims, and advisory work are deferred for one committed batch
- **THEN** each unfinished component retains exact custody for the batch generation
- **AND** the server drains the components without requiring an operator command or another foreground write

#### Scenario: Old worker completes after a newer write

- **WHEN** a worker finishes an older receipt revision after the same path has a newer revision
- **THEN** its completion CAS cannot clear the newer receipt
- **AND** stale component rows are not published as current

#### Scenario: Exact deferral is not promoted to whole-vault debt

- **WHEN** every required path for an unfinished component is covered by exact durable receipts
- **THEN** full-upsert accounting accepts that component's deferral
- **AND** it does not add a second full-scope receipt for the same demand

#### Scenario: Running server restarts with pending receipts

- **WHEN** a process starts with exact component receipts left by an earlier process
- **THEN** the component drain is scheduled without waiting for a watcher event or new mutation
- **AND** repeated failures back off while preserving the receipts and health telemetry

#### Scenario: Aborted batch releases its pending rows

- **WHEN** a prepared batch is retired as `aborted`
- **THEN** its pending-visibility rows are retired in the same transition
- **AND** they no longer count against the bounded hydration limit
