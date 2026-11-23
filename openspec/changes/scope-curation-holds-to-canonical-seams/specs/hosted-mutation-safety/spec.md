## ADDED Requirements

### Requirement: Curation Holds The Boundary Only At Canonical Seams

A curation apply or resume SHALL hold the vault mutation boundary only for each canonical write of a step and SHALL run its planning and probing outside it, revalidating under it. A step whose leaf does not check its sealed binding under its own guard SHALL run whole under one hold. `EXOMEM_WIDE_MUTATION_BOUNDARY` SHALL restore one hold for the whole call.

#### Scenario: The canonical writes are the only holds

- **WHEN** a self-guarded step runs
- **THEN** the boundary is held for the prepared-state write, the leaf's commit with its same-batch witness, and the receipt with the run's final state
- **AND** the check of the witness and of the live postcondition runs under the same short hold as the receipt

#### Scenario: A page changed after the leaf committed stays committed

- **WHEN** another writer changes a self-guarded step's page after the call saw its leaf's batch commit
- **THEN** that call records the step's receipt
- **AND** an undo reports the change as stale
- **AND** recovery from any other witness keeps the live check that it had under one wide hold

#### Scenario: A second call on a run in flight is refused

- **WHEN** a call finds the same run executing in its process
- **THEN** it is refused at once with a retryable `CURATION_RUN_BUSY` that commits nothing
- **AND** it does not wait for that run while it holds the boundary

#### Scenario: A note save during a slow curation step does not wait

- **WHEN** a curation step's leaf is still preparing its commit
- **AND** a note save on another page arrives
- **THEN** the note save commits without waiting for the curation step
- **AND** the curation step commits the same receipt, witness and page bytes as under one wide hold

#### Scenario: A stop between leaf commit and receipt is recovered by the next resume

- **WHEN** a curation step's leaf has committed
- **AND** the call stops before it records the receipt, for example because its settle hold is refused
- **THEN** the call reports that it committed
- **AND** the next resume records the step's receipt once from its witness and does not run the leaf again

#### Scenario: A save after a step's leaf commit leaves the step committed

- **WHEN** a self-guarded curation step's leaf has committed
- **AND** another writer saves the same page before the step records its receipt
- **THEN** the step records a committed receipt in this call
- **AND** the undo proposal reports the step's page as stale instead of leaving the step out

#### Scenario: A witness that a failed batch left behind does not prove the step

- **WHEN** an `edit`, `observe-unit`, `create-note`, `move` or `unmove` step's leaf batch fails after it wrote its witness
- **AND** the rollback restores the page but cannot remove the witness
- **THEN** the run blocks as uncertain and holds no committed receipt
- **AND** the page keeps its pre-step bytes

#### Scenario: A second call on a run in flight is refused

- **WHEN** a resume of a run is executing a step
- **AND** a second resume of the same run arrives in the same process
- **THEN** the second resume is refused with `CURATION_RUN_BUSY`, `status: retryable` and `committed: false`
- **AND** the step commits once
