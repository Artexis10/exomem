## MODIFIED Requirements

### Requirement: Readiness distinguishes serving from cutover

Runtime readiness SHALL keep the serving `ready` status and SHALL additionally report a cutover component set naming the lexical catalog, the embedding model when preload is allowed, the graph snapshot, and the semantic corpus context. The graph snapshot component SHALL be ready when the standby proved the snapshot, or when its proof declined because the snapshot is unusable for this build. A standby worker SHALL report which cutover component it is waiting on. After a promotion, readiness SHALL report which cutover components were carried forward from the standby. Queued vocabulary recovery SHALL drain in the background once the graph projection is current.

#### Scenario: A standby reports what it waits on
- **WHEN** a standby worker has warmed the lexical catalog but not yet proven the graph snapshot
- **THEN** readiness reports serving-ready and cutover-not-ready with `graph_snapshot` as the waiting component

#### Scenario: A standby that has not built the corpus is not cutover-ready
- **WHEN** a standby worker has proven its graph snapshot but has not built the semantic corpus context the write admission gate waits on
- **THEN** readiness reports cutover-not-ready with `semantic_corpus` as the waiting component

#### Scenario: Recovery drains without a review call
- **WHEN** vocabulary recovery rows were queued while the projection was warming and the projection becomes current
- **THEN** the activation sequence drains them within its bounded pass and no client review call is required
