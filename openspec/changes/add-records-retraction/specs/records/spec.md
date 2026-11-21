## ADDED Requirements

### Requirement: A committed record can be retracted with history
`record_memory(action="retract")` SHALL take a store-backed Records collection, a row id or item key, the current item and container guards, and a `why`. It SHALL take the row out of the collection's current state under the same write authority as an update, and SHALL keep every version and audit effect of the row. It SHALL write a `retract` audit effect that row history reports with its reason.

After retraction, queries, counts, inspect and row-id binding SHALL treat the row as absent. Retracting an absent or already retracted row SHALL be refused identically, `RECORD_NOT_FOUND`. A caller who cannot read the whole collection SHALL receive the same refusal as for an update. A stale guard SHALL be refused and SHALL write nothing.

A later write with the same natural key SHALL bring the row back into current state as its next version.

#### Scenario: Retracting a wrongly admitted row
- **WHEN** an owner retracts a row whose natural key holds a `null` component, with the current guards and a reason
- **THEN** the collection's current count falls by one, the row no longer appears in queries, its id is refused like an absent id, and its history shows its versions and the retraction with the reason

#### Scenario: Re-admitting a retracted row
- **WHEN** a later append or import writes a row with the natural key of a retracted row
- **THEN** the row is current again as its next version, and its history shows the retraction and the re-admission

#### Scenario: An absent row and a retracted row are indistinguishable
- **WHEN** an owner retracts an item key that names no row, and separately retracts an item key that was already retracted
- **THEN** both refusals are identical

### Requirement: Retraction is limited to collections whose current state is the store
Retraction SHALL be refused `RETRACT_UNAVAILABLE` on a file-canonical collection and on a collection that renders one view per row, after the collection resolves under the caller's authority. Derived collections SHALL keep their existing refusal.

#### Scenario: A file-canonical collection
- **WHEN** an owner asks to retract a row of a file-canonical Records collection
- **THEN** the request is refused `RETRACT_UNAVAILABLE` and nothing is written

### Requirement: Retraction keeps derived state and history consistent
When a row is retracted or re-admitted, the collection's typed rows, query projections, rollups, reference index, import row counts and history verification SHALL agree with its current rows. A cold reopen of the store SHALL verify the full history chain, including `retract` effects.

#### Scenario: A typed collection with an index, a rollup and an inverse reference
- **WHEN** an owner retracts a row of a typed collection that has a declared index, a rollup and a row that references it, and later re-admits it
- **THEN** after the retraction the typed query, the rollup and the reference resolve without the row, and after the re-admission they include it again

#### Scenario: History verifies after a retraction
- **WHEN** the store is reopened cold after a retraction
- **THEN** history verification succeeds from the first transaction, and a later write commits
