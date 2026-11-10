## ADDED Requirements

### Requirement: An Activation Carries Its Own Deadline Beneath The Request's

When the operator sets `EXOMEM_ACTIVATION_BUDGET_SECONDS`, an activation that runs inside an MCP request budget SHALL run on its own budget, a child of the request's. The child SHALL end that many seconds after the activation starts, or one second before the request's deadline when that is sooner. A client SHALL NOT set it per call. Unset, the activation SHALL run on the request's budget with no child, because a stage before task discovery still abstains at a deadline and a nearer one would turn calls that serve late into abstentions. A value that is not a positive number SHALL log one warning and bind no child. The child SHALL record every stage it skips or truncates on the request's budget too, so the request's budget names them. A call that carries no budget (local CLI, in-process callers, tests, and the REST and CLI doors that bind their own shorter activation budget) SHALL keep that behaviour and bind no child. The setting SHALL apply in the `maps` packet mode and SHALL NOT change the `anchors` rollback.

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
