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
