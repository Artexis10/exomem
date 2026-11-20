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

The media supervisor SHALL publish the durable results it drains as one batch under one vault mutation boundary hold, with one commit-generation advance and one post-commit fan-out for the whole batch. A batch SHALL stop at a size cap and at a time cap inside the hold. A batch SHALL land whole or roll back: a failed batch commit SHALL leave every result of the batch pending. The supervisor SHALL then bisect exactly the results of the failed batch, one half per pass, so that a result that cannot publish ends up alone and is set aside while the others publish, and no result is lost or published twice. A set-aside result SHALL stay set aside until its own result ends. A result that fails while it is staged SHALL stay pending without failing the batch. A refused boundary SHALL leave the batch pending, whole, and SHALL NOT start a hold for each result. The graph floor and its checkpoint SHALL be published inside the commit's own hold, and the post-commit fan-out SHALL name every page that the commit created. A media publication SHALL complete the graph incrementally and SHALL NOT schedule a full graph rebuild.

#### Scenario: A batch of results takes one hold and advances the generation once

- **WHEN** the supervisor publishes several recorded media results as one batch
- **THEN** the batch takes one boundary hold and advances the commit generation once
- **AND** every result's sidecar, index rows and graph pages are complete after the batch
- **AND** no result stays pending

#### Scenario: A failed batch rolls back and loses nothing

- **WHEN** the commit of a batch fails for one result
- **THEN** no sidecar of the batch changes and every result stays pending
- **AND** the next passes publish halves of exactly the failed results until the failing result is alone, and that result is set aside
- **AND** the other results publish once each, also when more results wait than one batch takes
- **AND** the set-aside result publishes once it has waited the progress floor and can commit

#### Scenario: A result that cannot be staged costs the others no second hold

- **WHEN** one result of a batch fails while it is staged
- **THEN** the other results commit in the same hold, with one advance
- **AND** the failing result stays pending

#### Scenario: A refused boundary leaves the batch pending

- **WHEN** the boundary is refused as busy for a batch of several results
- **THEN** no hold runs and no result changes
- **AND** the supervisor makes one attempt for the batch, not one for each result
- **AND** the next pass publishes the whole batch

#### Scenario: A reprocess that creates a page completes the graph incrementally

- **WHEN** a media publication creates a prior-extraction page
- **THEN** the post-commit fan-out names that page and the graph checkpoint is accepted
- **AND** the graph epoch is coherent and current
- **AND** no full graph rebuild is scheduled

### Requirement: Media publication waits for a quiet foreground gap

The media supervisor SHALL take the boundary for a batch only after foreground writes have been quiet for a fixed gap, as the foreground activity signal reports, and not within a fixed spacing of its previous hold. Any hold that ran SHALL set the spacing, whether it staged results or not and whether it committed or rolled back. A result that has waited longer than a progress floor SHALL publish at the next opportunity even when foreground writes never pause. The floor SHALL count from the later of the oldest result and the previous hold, so a result that cannot publish opens it once per floor, not on every pass. A deferred publication SHALL stay durable and visible: `process_media status` SHALL report the pending publication count and the age of the oldest pending result, and SHALL report the count as unknown when the queue cannot be read. Every budget SHALL be defined in one module.

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

### Requirement: A reprocess request queues without advancing the commit generation

A `process_media reprocess` request SHALL plan each path, including the hash of its file and the hydration of an evicted original, outside any vault mutation boundary hold. A path whose transcript is complete SHALL queue its job under a hold that does not advance the commit generation, because the job store is machine-local and no stored content changes. A path that needs a sidecar commit SHALL commit it under its own hold, which advances the generation as any canonical write does, and its index and graph fan-out SHALL run after that hold. A path whose queueing finds the media runtime unavailable SHALL run again under that advancing hold, so its page, its job and its result row all show the blocked state. In a store-mode vault the sidecar step runs on the store thread under its own advancing guard, so a path advances the generation once there. The request SHALL yield to waiting writes between paths.

#### Scenario: Six completed paths queue without an advance in a file-mode vault

- **WHEN** a reprocess request names six paths that have complete transcripts in a file-mode vault
- **THEN** the request does not advance the commit generation
- **AND** every path has its job queued

#### Scenario: Planning runs outside the boundary

- **WHEN** a reprocess request names paths with complete transcripts and a path that has no page yet
- **THEN** no file is hashed or hydrated while the boundary is held
- **AND** only the path that commits a new page advances the commit generation

#### Scenario: A runtime that goes down after the plan still blocks the path

- **WHEN** the media runtime becomes unavailable between a path's plan and its queueing hold
- **THEN** the path's page, job and result row all show the blocked state
- **AND** the queueing hold wrote no page
