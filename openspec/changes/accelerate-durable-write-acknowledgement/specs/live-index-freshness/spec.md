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

#### Scenario: A promoted component does not wait for the next tick

- **WHEN** a drain pass completes a component and the store promotes that component's dependants
- **THEN** the same pass claims the promoted dependants within its remaining allowance, so a batch converges through its embedding component in one normal-mode pass
- **AND** no dependant is claimed before every component it depends on has completed, and quiet mode's single-slot allowance still bounds the pass

#### Scenario: A registered graph rebuild does not hold the batch's other components

- **WHEN** the receipt-owned fan-out of a batch registers a whole-vault graph rebuild
- **THEN** it starts that rebuild and continues to the embedding component without waiting for the rebuild to finish
- **AND** the graph pipeline, not the fan-out, owns that rebuild's convergence and the `graph_sync` state

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
