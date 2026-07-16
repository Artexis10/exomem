## ADDED Requirements

### Requirement: Graph maintenance uses one operation-scoped resolver snapshot

The system SHALL acquire one detached wikilink-resolver snapshot before mutating the epistemic graph for one stable full-rebuild pass or one batched refresh, and SHALL reuse that snapshot for every page in the pass. Resolver acquisition and vault-wide freshness work MUST NOT scale with the number of pages in that pass. The snapshot MUST NOT warm or mutate the process-shared resolver cache.

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

### Requirement: Full rebuild stabilization is disk-truth and constant-bounded

The system SHALL bracket a full graph rebuild pass with direct on-disk vault
freshness keys and SHALL acquire its detached resolver against the pre-pass disk
key rather than potentially stale event-registry state. If freshness changes
during the first pass, it SHALL retry once with a new disk key and detached
resolver. It MUST perform at most two passes. If freshness changes during the
second pass, it SHALL mark the graph unavailable/non-current and raise instead
of exposing the unstable result as trusted.

#### Scenario: Target appears after snapshot acquisition

- **WHEN** a target file appears or is renamed after the first detached resolver is acquired
- **AND** the first graph pass observes changed disk freshness
- **THEN** the rebuild acquires a new detached resolver and retries
- **AND** the stable retry resolves source edges against the target's final path

#### Scenario: Vault moves during both bounded passes

- **WHEN** direct disk freshness changes during both the first and second graph passes
- **THEN** the rebuild performs exactly two resolver acquisitions and two passes
- **AND** it marks the graph unavailable/non-current
- **AND** it raises so later reconcile or refresh can rebuild the graph

#### Scenario: Stable rebuild retains linear work

- **WHEN** disk freshness is unchanged around the first graph pass
- **THEN** the rebuild performs one resolver acquisition and one graph pass
- **AND** resolver or freshness work does not scale per indexed page

### Requirement: Bounded graph maintenance preserves correctness and failure ordering

The system SHALL preserve the same graph nodes and edges produced from an equivalent stable vault, including ambiguous-link behavior. It MUST acquire initial disk freshness and the resolver before deleting or replacing existing graph rows so an initial acquisition failure leaves the prior graph intact. Once a rebuild pass begins, every exceptional exit MUST mark the graph unavailable/non-current before propagating, including pass failures, post-pass freshness failures, and freshness or resolver failures while acquiring a required retry.

#### Scenario: Resolver acquisition fails before graph mutation

- **WHEN** resolver snapshot acquisition fails at the start of a full graph rebuild
- **THEN** the rebuild reports the failure
- **AND** the previously committed graph rows remain unchanged

#### Scenario: Retry acquisition fails after a moved pass

- **WHEN** a completed graph pass observes changed disk freshness
- **AND** disk-freshness or resolver acquisition for the required retry fails
- **THEN** the rebuild marks the graph unavailable/non-current
- **AND** it propagates the acquisition failure

#### Scenario: Graph pass fails after mutation begins

- **WHEN** a full rebuild pass fails during indexing or its post-pass freshness check
- **THEN** the rebuild marks the partial graph unavailable/non-current
- **AND** it propagates the original failure

#### Scenario: Resolver reuse preserves graph output

- **WHEN** a stable fixture vault is rebuilt with an operation-scoped resolver snapshot
- **THEN** its graph node and edge payloads match the fresh-resolution reference for that vault

#### Scenario: Concurrent source changes remain eventually repairable

- **WHEN** a Markdown file changes after graph maintenance acquired its snapshot
- **THEN** the current operation remains internally consistent with that snapshot
- **AND** a later watcher event or reconcile operation can refresh the affected derived graph state
