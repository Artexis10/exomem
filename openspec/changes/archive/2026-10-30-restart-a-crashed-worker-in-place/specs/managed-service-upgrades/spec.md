## ADDED Requirements

### Requirement: A worker that exits outside a transition is replaced in place

When the serving worker exits outside a managed transition, the supervisor SHALL keep the public listener, the local listener and the control channel open, and SHALL start a replacement worker for the active release. Before the replacement starts, the supervisor SHALL prove that the exited worker and every descendant it left have exited. Until the replacement reports ready, the supervisor SHALL admit calls under the same bounds as a handoff.

The supervisor SHALL restart a serving worker in place at most a fixed number of times within a fixed window, and SHALL make one attempt per exit. The supervisor SHALL stop with a failed exit status in these cases, so that the service manager cleans the unit and applies its restart policy: a further exit inside the window, a descendant that is still alive when the stop allowance ends, and a replacement that does not report ready inside the cold-start window. A restart that waited for a transition SHALL NOT act when that transition already replaced the worker or left the supervisor in recovery.

The supervisor's status SHALL report a distinct phase while the replacement starts. It SHALL also report the restarts inside the window, the limit, the window and the last exit status.

#### Scenario: The worker crashes while a client is connected
- **WHEN** the serving worker is killed outside a transition
- **THEN** the public listener and the control channel stay open
- **AND** a call that arrives while the replacement warms is held and answered by the replacement once it reports ready
- **AND** the status reports the restart and the worker's exit status

#### Scenario: The crashed worker left a descendant
- **WHEN** a process that the worker started in its own session survives the worker's crash
- **THEN** that process is stopped and its exit is proven before the replacement starts

#### Scenario: A descendant outlives the stop allowance
- **WHEN** a descendant of the exited worker is still alive when the stop allowance ends
- **THEN** no replacement starts
- **AND** the supervisor stops with a failed exit status

#### Scenario: Crashes minutes apart
- **WHEN** worker exits are far enough apart that the restart limit is never reached inside one window
- **THEN** each exited worker is replaced in place

#### Scenario: The worker keeps exiting
- **WHEN** serving workers exit more times inside the window than the restart limit allows
- **THEN** the supervisor stops with a failed exit status, and the service manager's restart policy applies

#### Scenario: The replacement does not report ready
- **WHEN** the replacement exits before it reports ready, or does not report ready inside the cold-start window
- **THEN** the supervisor stops with a failed exit status instead of trying again

#### Scenario: A transition handled the exit first
- **WHEN** the worker exits while an upgrade holds the transition lock, and the upgrade then serves a new worker or records a failed transition
- **THEN** the restart does not stop, start or resume anything
- **AND** a failed transition stays in recovery with public ingress unavailable
