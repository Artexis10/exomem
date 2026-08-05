## ADDED Requirements

### Requirement: The embedding runtime is substitutable and provably equivalent

The bi-encoder SHALL be reachable through a backend seam so the serving runtime can be
substituted without changing retrieval behaviour or any calling code. A substituted
backend SHALL produce vectors of the same dimensionality and SHALL be proven equivalent
to the reference backend by cosine similarity on a fixed text set before it is shipped.

A backend substitution SHALL NOT require re-embedding an existing vault, and SHALL NOT
silently change which model produced stored vectors.

#### Scenario: Backend is substituted

- **WHEN** the embedding backend changes but the model identity and dimensionality do not
- **THEN** stored vectors remain valid and no rebuild is triggered
- **AND** an equivalence check on a fixed text set records the minimum cosine similarity

#### Scenario: Model identity changes

- **WHEN** a change would alter the model that produces vectors, not merely the runtime serving it
- **THEN** the stored embedding runtime fingerprint no longer matches
- **AND** the mismatch is surfaced rather than allowing two vector spaces to mix silently

### Requirement: The hosted cell carries only the runtime it serves with

The hosted image SHALL contain exactly one embedding runtime and the weights that runtime
loads, and SHALL NOT carry weights or frameworks for a capability whose grant is withheld.
The build SHALL prove the offline load with the runtime that actually serves requests.

A cell runs with no egress and a read-only root filesystem, so an unproven load path is
discovered by the first tenant to query by meaning rather than by the build.

#### Scenario: Image is built for the hosted lane

- **WHEN** the hosted image is built
- **THEN** the build loads the model through the serving runtime with the hub pinned offline and asserts an encode succeeds
- **AND** framework artifacts the serving runtime never reads are absent from the final image

#### Scenario: Cell memory envelope is set

- **WHEN** the embedding runtime changes the warm resident footprint of a cell
- **THEN** the cell resource requests and limits are re-derived from a fresh measurement
- **AND** the USER cell cap follows that measurement rather than the superseded envelope
