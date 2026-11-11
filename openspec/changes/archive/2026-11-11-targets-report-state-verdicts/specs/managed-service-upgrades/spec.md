## ADDED Requirements

### Requirement: State verdicts are computed under the target release

When deciding whether a target may serve the vault's state and whether a
handoff must run the state migrator, the supervisor SHALL obtain the verdict by
running the target release's own code under the target's interpreter, and SHALL
NOT interpret the state manifest's descriptor content with its own release's
format catalog. The verdict is one of serve, migrate with a reason, or refuse
with a reason. A target that reports refuse SHALL be refused as a handoff
candidate and SHALL NOT be spawned as the recorded active worker; a reported
migrate verdict SHALL drive the same migrator decision as a declared state
migration does today.

When the target cannot report a verdict and the supervisor's own state read
succeeds, the supervisor SHALL use its own read. When neither produces a
verdict, a handoff SHALL declare the migration — the migrator already runs
under the target interpreter — and a first start SHALL still spawn the recorded
active worker, whose own startup gate decides whether it can serve.

A supervisor that cannot read the vault's state manifest itself SHALL continue
to serve the recorded worker but SHALL refuse orchestrated upgrades and SHALL
name the recorded formats it does not recognize.

#### Scenario: A recorded worker's format postdates the supervisor

- **WHEN** the vault's state manifest records an optional format id the
  supervisor's release does not know, and the unit starts or restarts
- **THEN** the supervisor obtains the active target's verdict under the
  target's interpreter, starts the worker it names, and reaches the ready
  phase instead of exiting

#### Scenario: A rollback candidate cannot read the state

- **WHEN** a staged target's own code reports that it cannot serve the vault's
  recorded state formats
- **THEN** the upgrade is refused with the target's reason and the serving
  worker continues

#### Scenario: A target that cannot report

- **WHEN** a target predates the verdict probe and the supervisor's own state
  read succeeds
- **THEN** the supervisor decides with its own read, exactly as before

#### Scenario: Neither side can read the state

- **WHEN** the target cannot report a verdict and the supervisor cannot read
  the state manifest
- **THEN** a handoff declares the state migration so the target's migrator
  decides under its own interpreter, and a first start spawns the recorded
  active worker for its own gate to decide

#### Scenario: The active target reports it cannot serve

- **WHEN** the recorded active worker's own code reports that it cannot serve
  the vault's state
- **THEN** the supervisor does not spawn it and stops with a failed exit
  status

#### Scenario: Orchestration still requires the supervisor's own read

- **WHEN** the vault's state manifest records formats the supervisor's release
  does not recognize and an upgrade is requested
- **THEN** the upgrade is refused naming the unrecognized formats, as
  `upgrade_blocker` reports, while the recorded worker keeps serving
