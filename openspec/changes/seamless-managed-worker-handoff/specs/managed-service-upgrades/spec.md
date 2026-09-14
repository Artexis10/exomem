## MODIFIED Requirements

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
- **THEN** the candidate warms its lexical catalog, models when preload is allowed, and the proven graph snapshot before ingress is paused
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

## ADDED Requirements

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
