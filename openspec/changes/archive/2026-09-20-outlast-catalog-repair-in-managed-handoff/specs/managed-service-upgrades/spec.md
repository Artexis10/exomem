## ADDED Requirements

### Requirement: A stopped worker's replacement is awaited under the cold-start budget

Once the previous worker has been stopped, the system SHALL bound the wait for its replacement to report ready by the cold-start budget rather than by the cutover budget, on every path that reaches it: a promoted standby, a cold one-worker start, and the roll-forward of a recorded transition. The cutover budget SHALL continue to bound everything up to and including the offline migrator, because until the old worker is signalled the upgrade can still be abandoned and admission returned to a worker that is serving; past that point there is no such worker, so ending the wait early does not preserve availability.

The cold-start budget SHALL be one window, shared with the supervisor's own start, with a floor below which an operator cannot configure it.

A longer wait SHALL NOT weaken the failures that are cheap to detect. A replacement that exits before reporting readiness SHALL fail immediately and identify that it exited, and a replacement answering as a different release SHALL fail immediately. Only a replacement that is alive and serving the expected release SHALL be waited out.

A replacement that never reports ready inside the cold-start budget SHALL produce the same terminal outcome as before: the transition record is retained as failed, the supervisor enters recovery, public ingress is unavailable, owned processes are stopped with their exit proven, and a promotion that was accepted before the failure is preserved in the handoff record.

Admission during that wait SHALL remain bounded and explicit, with no request held without a bound, and the operator tooling's deadline for polling a transition SHALL cover the standby warm, the cutover and the cold-start budgets, so it does not report failure while the supervisor is still legitimately waiting. The handoff record SHALL report the time from the previous worker's stop to the replacement's readiness, on a failed handoff as well as a successful one, together with what the replacement last reported waiting on. Neither record SHALL affect any budget, wait or outcome.

#### Scenario: A write during the standby warm leaves the promoted worker repairing

- **WHEN** a client write lands while the candidate is warming, promotion reports the proved snapshot as `advanced`, and the promoted worker delegates its retrieval catalog to a background repair that outlasts the cutover budget
- **THEN** the supervisor keeps waiting for that worker's readiness inside the cold-start budget
- **AND** the upgrade completes and admission resumes to it
- **AND** the worker that already holds state is never stopped for being slow

#### Scenario: Resume waits out the same replacement

- **WHEN** a recorded failed transition is rolled forward and its replacement reports ready only after the cutover budget but inside the cold-start budget
- **THEN** the roll-forward completes, the transition record is accepted and the replacement serves
- **AND** the recovery does not stop a replacement whose warm was still progressing

#### Scenario: The replacement exits before readiness

- **WHEN** the replacement process exits before it reports ready
- **THEN** the upgrade fails immediately rather than waiting out the cold-start budget
- **AND** the failure identifies that the candidate exited before readiness

#### Scenario: The replacement never reports ready

- **WHEN** the replacement stays alive on the expected release but never reports ready
- **THEN** the upgrade fails at the end of the cold-start budget, not at the cutover budget
- **AND** the transition record is retained as failed, ingress is unavailable, the owned process is stopped and an accepted promotion is preserved in the handoff record
- **AND** the handoff record reports the window the replacement burned and what it last reported waiting on

#### Scenario: A request arrives while the replacement is still warming

- **WHEN** a request reaches the public endpoint while the supervisor is waiting for the replacement to report ready
- **THEN** it is held only within the admission wait budget and is then answered explicitly as not dispatched
- **AND** it is never queued for the length of the replacement's warm
