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
of exposing the unstable result as trusted. The schema-version marker MUST
remain absent throughout every pass and retry and MUST be published only after
the final direct disk-truth post-check succeeds.

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

#### Scenario: Stable completion is the only availability publisher

- **WHEN** a full rebuild is indexing pages or preparing a bounded retry
- **THEN** the graph schema-version marker remains absent
- **AND** only a successful final disk-freshness post-check publishes it

### Requirement: Incremental refresh does not own graph availability

The system SHALL allow incremental refresh to update graph rows and
registry/profile metadata but `_index_path` MUST NOT insert or restore the
schema-version marker. `refresh_paths` on a missing or unavailable sidecar MUST
route to a full rebuild so a partial path set is never advertised as a complete
current graph.

#### Scenario: Refresh contends with a full rebuild

- **WHEN** refresh and full rebuild are requested concurrently for one vault
- **THEN** one operation holds the shared mutation boundary
- **AND** the other waits or reports bounded `MUTATION_BUSY` without mutating graph rows or availability

#### Scenario: Refresh starts without a current sidecar

- **WHEN** `refresh_paths` starts with a missing or unavailable graph sidecar
- **THEN** it routes to a full rebuild
- **AND** availability is published only after the complete rebuild stabilizes

### Requirement: Graph mutations serialize across processes and accounts

The system SHALL serialize `rebuild_all`, `refresh_paths`, and `delete_paths`
through one re-entrant OS-backed mutation boundary keyed to the canonical vault
identity. The lock state MUST be rooted at
`<Knowledge Base>/.graph-coordination` rather than a per-account runtime root,
so service and interactive processes coordinate through the same lock file.
Acquisition SHALL use a bounded timeout of approximately 30 seconds. The held
boundary MUST include the initial availability decision, all row and metadata
mutation, stabilization checks, and final availability-marker publication.

#### Scenario: Cross-process mutation waits for an active rebuild

- **WHEN** one process is inside a full graph rebuild mutation boundary
- **AND** another process calls refresh or delete for the same canonical vault
- **THEN** the later mutator does not enter graph mutation until the rebuild releases the boundary

#### Scenario: Service and interactive accounts share authority

- **WHEN** two processes use different per-account runtime state directories but the same vault
- **THEN** all graph mutators resolve the lock beneath that vault's Knowledge Base
- **AND** they contend on the same canonical lock path

#### Scenario: Refresh routes to rebuild while holding the boundary

- **WHEN** refresh observes a missing or unavailable graph while holding mutation authority
- **THEN** it performs the required full rebuild without deadlocking or releasing authority between the decision and rebuild

#### Scenario: Older operation cannot reorder availability

- **WHEN** an older graph operation would otherwise publish or remove the schema marker after a newer overlapping mutation
- **THEN** serialization prevents the operations from overlapping
- **AND** marker state reflects completed mutation order

#### Scenario: Coordination state is outside the indexed corpus

- **WHEN** KB-only or full-vault Markdown traversal reaches the Knowledge Base
- **THEN** it excludes the `.graph-coordination` subtree before recursion
- **AND** lock state or its host ACLs do not participate in graph freshness or resolver input

#### Scenario: Lock acquisition cannot establish mutation authority

- **WHEN** a graph writer cannot acquire or open the shared mutation lock
- **THEN** it does not mutate graph rows or availability
- **AND** its structured lock error reaches index-sync reporting as a degraded graph component rather than accepted success

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
