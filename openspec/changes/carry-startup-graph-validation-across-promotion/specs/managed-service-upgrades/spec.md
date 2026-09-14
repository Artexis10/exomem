## MODIFIED Requirements

### Requirement: A promoted standby does not repeat the warm it already ran

A promoted worker SHALL NOT repeat warm-up work its own standby completed and promotion either re-verified or cannot invalidate, and SHALL record which components were carried forward. A component SHALL be carried only where the standby completed it; the adopted graph snapshot SHALL be carried only where promotion re-verified that the proved checkpoint is still current. Carried components SHALL be marked ready before the promoted worker's warm begins serving requests, so a governed write arriving immediately after promotion is admitted rather than refused as warming. A worker that starts cold SHALL carry nothing and SHALL run its whole warm unchanged. The watcher's start-up graph validation SHALL count as carried when the graph handoff was carried on a current verdict: the promoted worker SHALL NOT repeat the source-bytes proof its standby completed and promotion re-verified. Where start-up validation does run and finds the durable checkpoint unacknowledged while a governed mutation holds the boundary, it SHALL treat that as contention and wait for that mutation's own acknowledgement rather than a fixed budget, and SHALL NOT suspend reads or schedule a whole-vault rebuild on that evidence alone; only a proof that fails with the boundary free is incoherence.

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

#### Scenario: A governed write during the promoted worker's start-up validation is not incoherence
- **WHEN** the promoted worker's start-up validation observes an unacknowledged durable checkpoint while a governed write holds the mutation boundary for longer than the admission budget
- **THEN** validation waits for that write's acknowledgement, admits the graph it publishes, suspends no reads and schedules no whole-vault rebuild

#### Scenario: The promoted worker does not re-prove the carried graph
- **WHEN** a worker was promoted with the graph handoff carried on a current verdict
- **THEN** its watcher's start-up validation admits the graph on the durable checkpoint alone, without repeating the source-bytes proof, and the completion record names start-up validation among the carried components

