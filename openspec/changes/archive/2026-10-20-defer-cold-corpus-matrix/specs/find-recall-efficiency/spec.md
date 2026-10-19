## ADDED Requirements

### Requirement: A managed recall never loads the corpus matrix on its request thread

An activated managed-server `find` that needs the in-memory embedding matrix SHALL use a resident matrix that is current, or that the bounded catch-up makes current. When only a full load would serve, the request SHALL start at most one background load, omit the dense lane, and report the lane as `warming` with reason `matrix_warming`. The other lanes SHALL answer the request. The background load SHALL read the sidecar without holding the index lock that writers' cache updates take. While the last background load has failed and its backoff has not ended, the request SHALL report the dense lane as `failed` and SHALL NOT start a load. No caller SHALL start a second full load while one runs. An explicit offline caller with no activated runtime SHALL keep the inline load.

#### Scenario: A cold matrix loads once in the background

- **WHEN** an activated managed-server recall runs while no corpus matrix is resident
- **THEN** the response reports the dense lane as `warming` with reason `matrix_warming`
- **AND** exactly one background load runs, and a save during it does not wait for it
- **AND** a recall after the load completes uses the dense lane

#### Scenario: A matrix within the catch-up bound serves the request

- **WHEN** an activated managed-server recall runs while the resident matrix is behind by changes that the bounded catch-up covers
- **THEN** the request catches the matrix up and uses the dense lane
- **AND** no full load runs

#### Scenario: A failed background load is reported as failed

- **WHEN** the last background load failed and its backoff has not ended
- **THEN** an activated managed-server recall reports the dense lane as `failed`
- **AND** the request starts no load

#### Scenario: An offline caller loads inline

- **WHEN** `find` runs in an explicit offline caller with no activated runtime and no matrix is resident
- **THEN** the caller loads the matrix on its own thread and uses the dense lane
