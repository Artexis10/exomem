## ADDED Requirements

### Requirement: A managed save's write advisory never loads the corpus matrix on its request thread

In an activated managed-server runtime, a write advisory's overlap sweep that needs the in-memory embedding matrix SHALL use a resident matrix that is current, or that the bounded catch-up makes current. When only a full load would serve, the sweep SHALL start at most one background load and skip with reason `matrix_warming`, and the save SHALL proceed without the advisory. While the last background load has failed and its backoff has not ended, the sweep SHALL start no load and skip as failed. The receipt-owned deferred advisory SHALL follow the same rule and, when it skips, publish its result as failed. No sweep SHALL start a second full load while one runs. An explicit offline caller with no activated runtime SHALL keep the inline load.

#### Scenario: A save skips its advisory while a cold matrix loads

- **WHEN** an activated managed-server save runs its overlap advisory while no corpus matrix is resident
- **THEN** the advisory is skipped with reason `matrix_warming` and the save does not wait for a load
- **AND** exactly one background load runs
- **AND** a save after the load completes gets its overlap advisory

#### Scenario: An offline caller's advisory loads inline

- **WHEN** a write advisory runs in an explicit offline caller with no activated runtime and no matrix is resident
- **THEN** the caller loads the matrix on its own thread and scores the draft
