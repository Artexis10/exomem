## ADDED Requirements

### Requirement: An Activation Carries Its Own Deadline Beneath The Request's

When the operator sets `EXOMEM_ACTIVATION_BUDGET_SECONDS`, an activation that runs inside an MCP request budget SHALL run on its own budget, a child of the request's. The child SHALL end that many seconds after the activation starts, or one second before the request's deadline when that is sooner. A client SHALL NOT set it per call. Unset, the activation SHALL run on the request's budget with no child, because a nearer deadline would stop the turn slice's compile and serve the map alone where the request's own deadline lets the call serve its slice and leads late. A value that is not a positive number SHALL log one warning and bind no child. The child SHALL record every stage it skips or truncates on the request's budget too, so the request's budget names them. A call that carries no budget (local CLI, in-process callers, tests, and the REST and CLI doors that bind their own shorter activation budget) SHALL keep that behaviour and bind no child. The setting SHALL apply in the `maps` packet mode and SHALL NOT change the `anchors` rollback.

#### Scenario: The activation ends before the request does

- **WHEN** an MCP `activate_context` call carries the default 50-second budget and the operator set the activation budget to 15 seconds
- **THEN** the activation's own budget is 15 seconds and ends no later than one second before the request's deadline

#### Scenario: No activation budget is set

- **WHEN** an MCP `activate_context` call carries the default 50-second budget and the variable is unset
- **THEN** the activation binds no child and runs on the request's deadline

#### Scenario: The request's budget names what the activation left out

- **WHEN** the activation's budget skips or truncates `working_set.task_context_text`
- **THEN** the request's budget lists that stage as skipped or truncated, as the activation's budget recorded it

#### Scenario: Calls without a request budget are unchanged

- **WHEN** `activate_context` runs with no budget bound, or from a REST or CLI door
- **THEN** it binds no child and keeps its existing limit

## MODIFIED Requirements

### Requirement: The Budget Bounds Waits, Never Canonical Commits
The request budget SHALL bound waiting, optional work and the start of the first commit of a save the write intake can queue and the server can safely run again. A canonical mutation that has begun SHALL complete and persist its terminal regardless of the deadline. The derived acknowledgement deadline SHALL be the earlier of its entry-based value and the request deadline minus a delivery reserve, with `derived_sync` stating what is still behind.

#### Scenario: A commit under way is never interrupted
- **WHEN** the request deadline arrives while a canonical commit is executing
- **THEN** the commit completes, its terminal is persisted, and the response reports it as committed

#### Scenario: The acknowledgement deadline follows the request deadline
- **WHEN** a mutation enters the writer lease with 20 seconds of request budget remaining
- **THEN** the derived acknowledgement wait ends no later than 15 seconds later and the terminal reports `derived_sync` pending for anything not yet proven

#### Scenario: A save that used its call's time before its commit is queued
- **WHEN** a save the write intake can queue ends its preparation after its call's deadline and reaches its first write guard
- **THEN** the guard refuses with `MUTATION_BUSY`, `committed: false` and `cause: request_budget` before it acquires the boundary
- **AND** the save comes back queued and lands once

#### Scenario: A write the intake cannot queue still lands late
- **WHEN** a write the write intake cannot queue, or one that fetched a caller's temporary file links, ends its preparation after its call's deadline
- **THEN** the write commits as before

#### Scenario: A later guard of a commit under way still runs
- **WHEN** a call has acquired the vault mutation boundary once and its deadline then passes
- **THEN** a later write guard of the same call acquires the boundary as before
