## RENAMED Requirements

- FROM: `### Requirement: Every MCP Tool Call Carries A Request-Scoped Deadline`
- TO: `### Requirement: Every MCP Tool Call And Service Call Carries A Request-Scoped Deadline`

## MODIFIED Requirements

### Requirement: Every MCP Tool Call And Service Call Carries A Request-Scoped Deadline
The system SHALL create a request-scoped deadline for every MCP tool call and every service API call (`service-cell-contract`) at the dispatch entry, defaulting to 50 seconds after entry so the response leaves the origin before the client's 60-second tool timeout and the edge's 100-second cap, and SHALL expose the remaining budget to every stage on the request. The default SHALL be overridable by the operator through `EXOMEM_MCP_REQUEST_BUDGET_SECONDS`, which SHALL apply to both doors, and SHALL NOT be settable per call by the client. Calls that are neither MCP tool calls nor service API calls (local CLI, in-process callers, tests) and reconcile-class maintenance commands SHALL carry no budget and behave as before.

#### Scenario: A connector call gets the default budget
- **WHEN** an MCP tool call enters the dispatch path with no operator override set
- **THEN** a budget with a 50-second deadline from entry is readable by every stage the call runs

#### Scenario: A service call gets the same budget
- **WHEN** a service API call enters the dispatch path with no operator override set
- **THEN** a budget with a 50-second deadline from entry is readable by every stage the call runs

#### Scenario: The operator overrides the default
- **WHEN** `EXOMEM_MCP_REQUEST_BUDGET_SECONDS` is set to a positive number
- **THEN** new MCP tool calls and service API calls carry that budget
- **AND** a malformed value falls back to the default with one logged warning

#### Scenario: A client cannot loosen the budget
- **WHEN** a client passes any argument attempting to extend its budget
- **THEN** the tool schema or the service request schema does not accept it and the budget is unchanged

#### Scenario: Local and reconcile-class calls are exempt
- **WHEN** `find` runs from the CLI, or `reconcile` or `maintain_memory` in reconcile mode runs through MCP
- **THEN** no budget applies and no stage is skipped or truncated
