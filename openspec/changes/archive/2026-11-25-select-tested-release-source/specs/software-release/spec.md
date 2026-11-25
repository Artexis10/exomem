## ADDED Requirements

### Requirement: The release command selects the newest fully tested source

The supported release command SHALL select, by default, the newest commit on main's first-parent history since the preceding release reconciliation that has successful, authentic Full evidence the cut can reuse. When no such commit exists, it SHALL select main's tip, and the cut SHALL run its Full fallback. The selection SHALL report the reused evidence run, or report that none exists. The cut controller SHALL apply its own source and evidence checks to the selected source.

#### Scenario: A tested commit exists since the last release
- **WHEN** main's tip has no Full evidence and an earlier commit since the preceding reconciliation has a successful Full run whose receipt matches its tree
- **THEN** the release command selects that earlier commit and names its evidence run
- **AND** commits after it wait for the next release

#### Scenario: No tested commit exists since the last release
- **WHEN** no commit since the preceding reconciliation has reusable Full evidence, including when the only green Full run predates that reconciliation
- **THEN** the release command selects main's tip and reports no evidence run
- **AND** the cut dispatches its Full fallback as before

### Requirement: Main keeps recent Full evidence for release cuts

After each landing on main, CI SHALL dispatch one Full run on main when no Full run on main or on a release cut is queued or running, the newest such run started more than a configurable window ago, and main has moved since that run's commit. A window of 0 SHALL disable the dispatch.

#### Scenario: Old evidence and a moved main
- **WHEN** the newest Full run on main is older than the window, none is running, and main has moved since its commit
- **THEN** CI dispatches one Full run on main

#### Scenario: Recent, running or unchanged evidence
- **WHEN** a Full run on main or on a release cut is running, started within the window, or tested main's current commit
- **THEN** CI dispatches nothing
