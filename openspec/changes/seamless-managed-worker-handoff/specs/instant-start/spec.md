## ADDED Requirements

### Requirement: Governed writes are never fenced into a whole-vault rebuild by unattributed filesystem events

The projection-freshness fence raised by filesystem events the process cannot attribute to itself SHALL apply only to reads that require a current projection. A governed write SHALL always compute its predecessor from the checkpoint lineage. An unreadable predecessor SHALL be reported as a distinct outcome and routed to bounded incremental repair of the affected paths, never collapsed into the lineage-gap outcome that schedules a whole-vault rebuild. External marks SHALL be recorded per path and drained by the incremental repair.

#### Scenario: Writes after a worker replacement stay incremental
- **WHEN** a replacement worker starts with an empty self-attribution table and observes filesystem events from the previous worker and the migrator, and then serves ten governed writes
- **THEN** none of those writes schedules a whole-vault rebuild, each acknowledges within the incremental bound, and the repair queue drains to zero

#### Scenario: An unrelated external edit does not fence a write
- **WHEN** an unattributed edit lands on one path while a governed write commits to another
- **THEN** the write takes the incremental path, the edited path is queued for repair, and reads that require a current projection continue to refuse until that repair lands

#### Scenario: A real lineage gap still rebuilds
- **WHEN** the checkpoint lineage proves the snapshot cannot be advanced incrementally
- **THEN** a whole-vault rebuild is scheduled and the outcome names the lineage gap, not an unreadable predecessor

### Requirement: Joins on graph work are bounded and rebuild demand coalesces

No caller SHALL wait on graph registration without a budget. The budget SHALL derive from the request deadline when one exists and from a bounded default otherwise; past it the caller receives a pending outcome carrying the checkpoint it can poll. Rebuild demand that arrives while a whole-vault rebuild is in flight SHALL coalesce into at most one follow-up rebuild under the single-flight owner.

#### Scenario: A standalone caller cannot block for a rebuild
- **WHEN** a caller that cannot carry a pending outcome commits a write while a whole-vault rebuild is in flight
- **THEN** it receives its acknowledgement within the budget with a pending graph outcome and the checkpoint to poll

#### Scenario: Ten writes during one flight yield one follow-up
- **WHEN** ten governed writes commit while one whole-vault rebuild is running
- **THEN** at most one further whole-vault rebuild runs after it and every write is registered by that pass

### Requirement: Readiness distinguishes serving from cutover

Runtime readiness SHALL keep the serving `ready` status and SHALL additionally report a cutover component set naming the lexical catalog, the embedding model when preload is allowed, and the proven graph snapshot. A standby worker SHALL report which cutover component it is waiting on. Queued vocabulary recovery SHALL drain in the background once the graph projection is current.

#### Scenario: A standby reports what it waits on
- **WHEN** a standby worker has warmed the lexical catalog but not yet proven the graph snapshot
- **THEN** readiness reports serving-ready and cutover-not-ready with `graph_snapshot` as the waiting component

#### Scenario: Recovery drains without a review call
- **WHEN** vocabulary recovery rows were queued while the projection was warming and the projection becomes current
- **THEN** the activation sequence drains them within its bounded pass and no client review call is required
