## ADDED Requirements

### Requirement: An Activation Carries Its Own Deadline Beneath The Request's

An activation that runs inside an MCP request budget SHALL run on its own budget, a child of the request's. The child SHALL end `EXOMEM_ACTIVATION_BUDGET_SECONDS` seconds after the activation starts, 15 by default, or one second before the request's deadline when that is sooner. The operator SHALL set the value, and a client SHALL NOT set it per call. A value that is not a positive number SHALL log one warning and use the default. The child SHALL record every stage it skips or truncates on the request's budget too, so the request's budget names them. A call that carries no budget (local CLI, in-process callers, tests, and the REST and CLI doors that bind their own shorter activation budget) SHALL keep that behaviour and bind no child. The setting SHALL apply in the `maps` packet mode and SHALL NOT change the `anchors` rollback.

#### Scenario: The activation ends before the request does

- **WHEN** an MCP `activate_context` call carries the default 50-second budget and no override is set
- **THEN** the activation's own budget is 15 seconds and ends no later than one second before the request's deadline

#### Scenario: The request's budget names what the activation left out

- **WHEN** the activation's budget skips or truncates `working_set.task_context_text`
- **THEN** the request's budget lists that stage as truncated

#### Scenario: Calls without a request budget are unchanged

- **WHEN** `activate_context` runs with no budget bound, or from a REST or CLI door
- **THEN** it binds no child and keeps its existing limit
