## ADDED Requirements

### Requirement: Speech in worker mode stays pending for a worker
When a deployment sets its speech mode to `worker`, the media worker SHALL treat speech as handled outside the process: a speech job SHALL stay pending with an actionable waiting state and SHALL NOT be reported as blocked or failed while it waits. Reconciliation SHALL NOT create a second job for a recording that already has a pending or leased speech job.

#### Scenario: Waiting is not failure
- **WHEN** a recording's speech job has waited a day for a worker
- **THEN** its state is pending, its reason names the worker, and no failure is recorded

#### Scenario: Reconciliation keeps one job
- **WHEN** periodic reconciliation runs while a speech job is leased to a worker
- **THEN** no second speech job is enqueued for that recording
