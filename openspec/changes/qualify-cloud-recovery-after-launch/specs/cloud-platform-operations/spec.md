## ADDED Requirements

### Requirement: Recovery paths are qualified by recorded drills

The platform SHALL claim a recovery path only after a recorded drill on the current deployment shows that the path restores canonical vault and host state.
The recovery paths are historical rollback, in-place restore, recovery after node loss, a server move and the admission envelope.
A path without such a record SHALL be reported as unqualified, never as working.

#### Scenario: A path has no recorded drill
- **WHEN** an operator or a status report lists the platform's recovery paths
- **THEN** each path without a recorded drill on the current deployment is shown as unqualified

#### Scenario: A drill shows a path works
- **WHEN** a drill restores a QA cell and its comparison matches canonical vault and host state
- **THEN** the drill's record qualifies that path for the deployment it ran on

### Requirement: Recovery drills spare tenant data

A recovery drill SHALL run on a QA cell, a copy or approved disposable infrastructure, never on a tenant's live data.
A drill record SHALL name its deployment, its inputs, its comparison and its outcome.

#### Scenario: A drill needs a cell to damage
- **WHEN** a drill must damage, roll back or move a cell
- **THEN** it uses a QA cell or a copy, and every tenant cell keeps serving unchanged
