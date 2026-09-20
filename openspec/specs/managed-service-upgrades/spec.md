# managed-service-upgrades Specification

## Purpose
Keep a managed Linux/WSL service available to existing clients while replacing its single state-owning worker, with bounded requests and recoverable operator control.

## Requirements

### Requirement: Managed upgrades preserve the public endpoint

The system SHALL keep the public HTTP listener and existing accepted connections alive throughout a managed worker upgrade. Release download and installation SHALL finish before the existing worker stops accepting requests. The replacement SHALL warm as a standby beside the serving worker, reaching cutover readiness under a warm budget distinct from the cutover budget, before the existing worker stops; a standby that does not reach cutover readiness inside its budget SHALL be discarded with a recorded reason while the existing worker keeps serving. A worker SHALL be spawned with the unit's environment file as it stands at spawn time, so a changed service environment reaches the next worker without restarting the supervisor. Detached client streams SHALL be reattached with bounded retries through promotion rather than closed on the first non-success.

#### Scenario: Calls arrive during replacement
- **WHEN** calls arrive while a managed worker is being replaced within the configured handoff budget
- **THEN** the public endpoint accepts and holds them until the verified replacement is ready
- **AND** each admitted request is forwarded once

#### Scenario: Preparation fails
- **WHEN** release staging or target verification fails before handoff
- **THEN** the existing worker continues serving without a restart

#### Scenario: Standby warms while the old worker serves
- **WHEN** a managed upgrade starts against a serving worker
- **THEN** the candidate warms its lexical catalog, models when preload is allowed, the proven graph snapshot and the semantic corpus context before ingress is paused
- **AND** the unavailable window of the cutover is the drain plus promotion, not a cold start

#### Scenario: The service environment changed after the supervisor started
- **WHEN** the unit's environment file changed after the supervisor read it through systemd
- **THEN** the standby is spawned with the current file's values
- **AND** the supervisor's own environment is unchanged

#### Scenario: Standby misses its warm budget
- **WHEN** the candidate does not report cutover readiness inside the warm budget
- **THEN** it is stopped, the handoff record names the waiting component, and the existing worker keeps serving

#### Scenario: A stream survives promotion
- **WHEN** a long-lived client stream is detached for cutover and the first reattachment attempt fails while the standby is being promoted
- **THEN** the ingress retries within the cutover budget and the client's stream resumes on the promoted worker

### Requirement: Upgrades retain operation ownership
The system SHALL let forwarded finite requests finish before stopping their worker. It SHALL NOT automatically replay a request that was forwarded or treat a client disconnect as proof that an operation stopped.

#### Scenario: Mutation is active
- **WHEN** a mutation is executing as an upgrade begins
- **THEN** the upgrade waits for its complete response before stopping the worker
- **AND** the mutation executes once

#### Scenario: Client disconnects after dispatch
- **WHEN** a client disconnects after its operation reached the worker
- **THEN** that operation remains part of the drain until the upstream response completes

#### Scenario: Drain deadline expires
- **WHEN** finite requests do not finish within the drain budget
- **THEN** the upgrade aborts before signalling the worker and reopens admission to that worker

### Requirement: Handoff admission is bounded and explicit
The system SHALL bound queued request count, memory, body intake time and wait time. A refused request SHALL remain undispatched and receive a protocol-compatible error that identifies this fact when its request identity is available.

#### Scenario: Queue expires
- **WHEN** a queued MCP request exceeds the wait budget
- **THEN** it receives an error saying it was not dispatched
- **AND** the same connected client can make a subsequent call after service recovery

#### Scenario: Admission capacity is exhausted
- **WHEN** a new request exceeds the count or byte bounds
- **THEN** it is refused without executing a tool or allocating unbounded memory

### Requirement: Only one worker owns state during an upgrade
The system SHALL prove the previous worker and its owned descendants have exited before running offline migration or starting its replacement. The upgrade SHALL preserve the configured vault, external state root, issuer and OAuth authority.

#### Scenario: Old child survives shutdown
- **WHEN** an owned child remains alive after the worker exits
- **THEN** offline migration and replacement startup remain blocked until that child is stopped and its exit proven

#### Scenario: Replacement fails
- **WHEN** migration or replacement readiness fails after the old worker stopped
- **THEN** a durable recovery record identifies the incomplete transition
- **AND** neither an old release nor a second worker is silently started

### Requirement: Upgrade control is private and recoverable
The system SHALL expose upgrade control only through an owner-restricted local channel, serialize transitions and retain their outcome independently of the control client's connection.

#### Scenario: Remote control attempt
- **WHEN** a public HTTP client attempts to invoke an upgrade command
- **THEN** no upgrade control route or executable-selection surface is available

#### Scenario: Operator disconnects
- **WHEN** the operator connection closes after an upgrade is accepted
- **THEN** the supervisor completes or records the failed transition and reports its state to a later status request

#### Scenario: Supervisor restarts mid-transition
- **WHEN** the supervisor starts with an incomplete upgrade record
- **THEN** it does not launch the prior release and offers explicit roll-forward recovery

### Requirement: A standby holds no state ownership until promotion

A standby worker SHALL take no writer lease, publish no index or graph state, schedule no drain and own no descendants until it is promoted. Promotion SHALL happen only after the previous worker and its descendants have provably exited and after offline migration has run or been recorded as skipped because the target release declares none. On promotion the standby SHALL re-validate that the snapshot it proved is still current and SHALL re-prove or rebuild through the coalesced path when it is not, recording which happened.

#### Scenario: Standby cannot write
- **WHEN** a request would reach a standby worker before promotion
- **THEN** ingress does not route it there and the standby holds no lease that could accept it

#### Scenario: Migration is skipped only by declaration
- **WHEN** the staged target declares no state migration
- **THEN** the offline migrator step is recorded as skipped and promotion proceeds without it
- **AND** a target that declares a migration still runs it with no worker owning state

#### Scenario: State changed under the standby
- **WHEN** the offline migrator changes state after the standby proved its snapshot
- **THEN** the standby re-proves before promotion and, if the proof fails, rebuilds after promotion through the coalesced path with the handoff record naming the reason

### Requirement: A promoted standby does not repeat the warm it already ran

A promoted worker SHALL NOT repeat warm-up work its own standby completed and promotion either re-verified or cannot invalidate, and SHALL record which components were carried forward. A component SHALL be carried only where the standby completed it; the adopted graph snapshot SHALL be carried only where promotion re-verified that the proved checkpoint is still current. Carried components SHALL be marked ready before the promoted worker's warm begins serving requests, so a governed write arriving immediately after promotion is admitted rather than refused as warming. A worker that starts cold SHALL carry nothing and SHALL run its whole warm unchanged.

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

### Requirement: A stopped worker's replacement is awaited under the cold-start budget

Once the previous worker has been stopped, the system SHALL bound the wait for its replacement to report ready by the cold-start budget rather than by the cutover budget, on every path that reaches it: a promoted standby, a cold one-worker start, and the roll-forward of a recorded transition. The cutover budget SHALL continue to bound everything up to and including the offline migrator, because until the old worker is signalled the upgrade can still be abandoned and admission returned to a worker that is serving; past that point there is no such worker, so ending the wait early does not preserve availability.

The cold-start budget SHALL be one window, shared with the supervisor's own start, with a floor below which it cannot be configured.

A longer wait SHALL NOT weaken the failures that are cheap to detect. A replacement that exits before reporting readiness SHALL fail immediately and identify that it exited, and a replacement answering as a different release SHALL fail immediately. Only a replacement that is alive and serving the expected release SHALL be waited out.

A replacement that never reports ready inside the cold-start budget SHALL produce the same terminal outcome as before: the transition record is retained as failed, the supervisor enters recovery, public ingress is unavailable, owned processes are stopped with their exit proven, and a promotion that was accepted before the failure is preserved in the handoff record.

Admission during that wait SHALL remain bounded and explicit, with no request held without a bound, and the operator tooling's deadline for polling a transition SHALL cover the standby warm, the cutover and the cold-start budgets, so it does not report failure while the supervisor is still legitimately waiting. The handoff record SHALL report the time from the previous worker's stop to the replacement's readiness.

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

#### Scenario: A request arrives while the replacement is still warming

- **WHEN** a request reaches the public endpoint while the supervisor is waiting for the replacement to report ready
- **THEN** it is held only within the admission wait budget and is then answered explicitly as not dispatched
- **AND** it is never queued for the length of the replacement's warm
