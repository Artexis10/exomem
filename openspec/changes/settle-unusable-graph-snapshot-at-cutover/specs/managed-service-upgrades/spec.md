## MODIFIED Requirements

### Requirement: A promoted standby does not repeat the warm it already ran

A promoted worker SHALL NOT repeat warm-up work its own standby completed and promotion either re-verified or cannot invalidate, and SHALL record which components were carried forward. A component SHALL be carried only where the standby completed it; the adopted graph snapshot SHALL be carried only where promotion re-verified that the proved checkpoint is still current. Carried components SHALL be marked ready before the promoted worker's warm begins serving requests, so a governed write arriving immediately after promotion is admitted rather than refused as warming. A worker that starts cold SHALL carry nothing and SHALL run its whole warm unchanged. A graph snapshot that the standby proved unusable for its build SHALL be reported settled, so it does not hold cutover, and SHALL NOT be carried.

#### Scenario: A governed write arrives immediately after promotion
- **WHEN** a governed write reaches a worker that was promoted from a standby whose warm completed
- **THEN** it is admitted rather than refused as warming, and it is served incrementally

#### Scenario: The promoted worker's warm subtracts what it carried
- **WHEN** a promoted worker runs its own warm-up after a promotion that re-verified the snapshot as current
- **THEN** it neither adopts the snapshot again nor rebuilds the semantic corpus context, and its completion record names the carried components

#### Scenario: The snapshot moved under the standby
- **WHEN** promotion finds the checkpoint the standby proved is no longer current
- **THEN** the graph handoff is not carried forward and the promoted worker's warm runs it in full

#### Scenario: A cold start carries nothing
- **WHEN** a worker starts without having been a standby
- **THEN** it carries no components and runs every warm-up step

#### Scenario: A warm step failed rather than not running
- **WHEN** a standby's semantic corpus build runs and fails
- **THEN** the component is reported settled so the upgrade is not held, and it is not carried forward, so the promoted worker runs that step itself

#### Scenario: The inherited graph snapshot belongs to an older graph schema
- **WHEN** a standby proves that the inherited graph sidecar has an older graph schema version than its own build
- **THEN** the graph snapshot component is reported settled so the upgrade is not held
- **AND** promotion records the snapshot as unproven, and the promoted worker runs the whole graph handoff
