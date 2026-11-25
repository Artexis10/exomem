## MODIFIED Requirements

### Requirement: Main keeps recent Full evidence for release cuts

After each landing on main, CI SHALL dispatch one Full run on main when no Full run on main or on a release cut is queued or running, none has tested main's current commit, no release cut is open, and main's newest Full run started more than a configurable window ago. A window of 0 SHALL disable the dispatch.

#### Scenario: Old evidence and a moved main
- **WHEN** main's newest Full run is older than the window, no Full run is running, no cut is open, and no Full run has tested main's current commit
- **THEN** CI dispatches one Full run on main

#### Scenario: Recent, running or unchanged evidence
- **WHEN** a Full run on main or on a release cut is running, main's newest Full run started within the window, or a Full run tested main's current commit
- **THEN** CI dispatches nothing

#### Scenario: A cut is open
- **WHEN** a release cut's controller run is in progress
- **THEN** CI dispatches nothing, because a Full run before the cut's reconciliation cannot serve the next release

#### Scenario: A cut's Full run on an older commit
- **WHEN** a cut's Full run on a commit before its reconciliation finished within the window and main's own newest Full run is older than the window
- **THEN** the cut's run does not hold off the dispatch
