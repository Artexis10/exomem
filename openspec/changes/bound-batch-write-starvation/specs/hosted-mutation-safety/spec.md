## ADDED Requirements

### Requirement: Foreground writes are not starved by batch or background media work

A keyed write's place in the arrival-order queue SHALL end when its attempt holds the vault mutation boundary, not when its command returns. A multi-unit writer (a multi-path media request or the background media worker) SHALL give way between units to a keyed write that waits in line or at the boundary, for at most a bounded wait per unit, so batch work slows but never stops. Work that publishes a commit's derived registries SHALL NOT keep the boundary held past a short fixed bound: when it cannot finish within the bound, it SHALL complete after the boundary is released, in commit order, closing the commit's publication intents itself, and the boundary SHALL NOT be held for a cold whole-vault corpus build beyond that bound.

#### Scenario: A keyed edit during a multi-path reprocess commits within about one unit's hold

- **WHEN** a multi-path `process_media` request is committing its paths one at a time
- **AND** a keyed edit arrives while a unit is in flight
- **THEN** the edit acquires the boundary within about one unit's hold plus margin
- **AND** the batch completes every path

#### Scenario: A keyed write is not refused while the boundary sits idle

- **WHEN** a keyed write's command runs longer than a bounded wait while holding no boundary
- **AND** another keyed write arrives behind it
- **THEN** the later write's turn does not wait for the earlier command to return
- **AND** neither write is refused `capture_wait_exhausted` while the boundary is idle

#### Scenario: A blocked publication still ends the boundary hold

- **WHEN** a commit's derived-registry publication cannot finish within the short fixed bound
- **THEN** the boundary releases within that bound
- **AND** the publication completes afterwards, in commit order with the commits around it, closing the commit's publication intents itself
- **AND** the handed-off publication is not republished inside the boundary and mints no full-index receipt
- **AND** the boundary is not held for a cold whole-vault corpus build beyond that bound

### Requirement: Media publication lands in whole bounded batches

The media supervisor SHALL publish the durable results it drains as one batch under one vault mutation boundary hold, with one commit-generation advance and one post-commit fan-out for the whole batch. A batch SHALL stop at a size cap and at a time cap inside the hold. A batch SHALL land whole or roll back: a failed batch commit SHALL leave every result of the batch pending, and the supervisor SHALL then publish its results one at a time, so no result is lost or published twice. The graph floor and its checkpoint SHALL be published inside the commit's own hold, and the post-commit fan-out SHALL name every page that the commit created. A media publication SHALL complete the graph incrementally and SHALL NOT schedule a full graph rebuild.

#### Scenario: A batch of results takes one hold and advances the generation once

- **WHEN** the supervisor publishes several recorded media results as one batch
- **THEN** the batch takes one boundary hold and advances the commit generation once
- **AND** every result's sidecar, index rows and graph pages are complete after the batch
- **AND** no result stays pending

#### Scenario: A failed batch rolls back and loses nothing

- **WHEN** the commit of a batch fails for one result
- **THEN** no sidecar of the batch changes
- **AND** the supervisor publishes the results again one at a time
- **AND** each result publishes once, and only the failing result stays pending

#### Scenario: A reprocess that creates a page completes the graph incrementally

- **WHEN** a media publication creates a prior-extraction page
- **THEN** the post-commit fan-out names that page and the graph checkpoint is accepted
- **AND** the graph epoch is coherent and current
- **AND** no full graph rebuild is scheduled

### Requirement: Media publication waits for a quiet foreground gap

The media supervisor SHALL take the boundary for a batch only after foreground writes have been quiet for a fixed gap, as the foreground activity signal reports, and not within a fixed spacing of its previous batch. A result that has waited longer than a progress floor SHALL publish at the next opportunity even when foreground writes never pause. A deferred publication SHALL stay durable and visible: `process_media status` SHALL report the pending publication count and the age of the oldest pending result. Every budget SHALL be defined in one module.

#### Scenario: Foreground activity defers a batch

- **WHEN** a foreground write is active or recent
- **THEN** the supervisor does not take the boundary for a batch
- **AND** `process_media status` reports the pending count and the oldest age

#### Scenario: Continuous foreground traffic cannot starve publication

- **WHEN** foreground writes never pause for a quiet gap
- **AND** the oldest result has waited longer than the progress floor
- **THEN** the supervisor publishes a batch

#### Scenario: Batches are spaced

- **WHEN** a batch committed less than the fixed spacing ago
- **THEN** the supervisor defers the next batch until the spacing has passed

### Requirement: A multi-path reprocess request queues under one hold

A `process_media reprocess` request over several paths SHALL queue the paths whose transcript is complete under one boundary hold, advancing the commit generation once for that hold, up to a time cap, and SHALL yield to waiting writes between holds. A path that needs a sidecar commit SHALL commit it with its own hold, so no index or graph fan-out runs inside the shared hold.

#### Scenario: Six completed paths queue with one advance

- **WHEN** a reprocess request names six paths that have complete transcripts
- **THEN** the request advances the commit generation once
- **AND** every path has its job queued
