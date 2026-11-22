## ADDED Requirements

### Requirement: Curation Holds The Boundary Only At Canonical Seams

A curation apply or resume SHALL hold the vault mutation boundary only for each canonical write of a step, and SHALL run its planning and probing outside it, revalidating under it. The canonical writes are the prepared-state write, the leaf's commit with its same-batch witness, and the receipt with the run's final state. The check of the witness and of the live postcondition SHALL run under the same short hold as the receipt. A step whose leaf does not take the boundary itself and check its sealed binding under it SHALL run whole under one hold. A call that finds the same run executing in its process SHALL be refused at once with a retryable `CURATION_RUN_BUSY` that commits nothing, and SHALL NOT wait for that run while it holds the boundary. `EXOMEM_WIDE_MUTATION_BOUNDARY` SHALL restore one hold for the whole call.

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

#### Scenario: A second call on a run in flight is refused

- **WHEN** a resume of a run is executing a step
- **AND** a second resume of the same run arrives in the same process
- **THEN** the second resume is refused with `CURATION_RUN_BUSY`, `status: retryable` and `committed: false`
- **AND** the step commits once
