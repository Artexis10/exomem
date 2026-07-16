## ADDED Requirements

### Requirement: Graph maintenance uses one operation-scoped resolver snapshot

The system SHALL acquire one detached wikilink-resolver snapshot before mutating the epistemic graph for a full rebuild or one batched refresh, and SHALL reuse that snapshot for every page in the operation. Resolver acquisition and vault-wide freshness work MUST NOT scale with the number of pages in that operation. The snapshot MUST NOT warm or mutate the process-shared resolver cache.

#### Scenario: Full rebuild has bounded resolver work

- **WHEN** a full graph rebuild indexes multiple Markdown pages in a process without a live freshness registry
- **THEN** it acquires one detached resolver snapshot for the operation
- **AND** it does not perform a vault-wide resolver freshness check for each page

#### Scenario: Batched refresh has bounded resolver work

- **WHEN** one `refresh_paths` operation indexes multiple changed Markdown pages
- **THEN** it acquires one detached resolver snapshot and reuses it for the batch
- **AND** a later separate `refresh_paths` operation acquires a new snapshot

#### Scenario: Shared resolver changes do not mutate an active graph snapshot

- **WHEN** the process-shared resolver is patched after graph maintenance has acquired its detached snapshot
- **THEN** the active operation continues resolving every page against its original snapshot

### Requirement: Bounded graph maintenance preserves correctness and failure ordering

The system SHALL preserve the same graph nodes and edges produced from an equivalent stable vault, including ambiguous-link behavior. It MUST acquire the resolver before deleting or replacing existing graph rows so a resolver acquisition failure leaves the prior graph intact.

#### Scenario: Resolver acquisition fails before graph mutation

- **WHEN** resolver snapshot acquisition fails at the start of a full graph rebuild
- **THEN** the rebuild reports the failure
- **AND** the previously committed graph rows remain unchanged

#### Scenario: Resolver reuse preserves graph output

- **WHEN** a stable fixture vault is rebuilt with an operation-scoped resolver snapshot
- **THEN** its graph node and edge payloads match the fresh-resolution reference for that vault

#### Scenario: Concurrent source changes remain eventually repairable

- **WHEN** a Markdown file changes after graph maintenance acquired its snapshot
- **THEN** the current operation remains internally consistent with that snapshot
- **AND** a later watcher event or reconcile operation can refresh the affected derived graph state
