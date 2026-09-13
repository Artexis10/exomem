## ADDED Requirements

### Requirement: Every MCP Tool Call Carries A Request-Scoped Deadline
The system SHALL create a request-scoped deadline for every MCP tool call at the dispatch entry, defaulting to 50 seconds after entry so the response leaves the origin before the client's 60-second tool timeout and the edge's 100-second cap, and SHALL expose the remaining budget to every stage on the request. The default SHALL be overridable by the operator through `EXOMEM_MCP_REQUEST_BUDGET_SECONDS` and SHALL NOT be settable per call by the client. Calls that are not MCP tool calls (local CLI, in-process callers, tests) and reconcile-class maintenance commands SHALL carry no budget and behave as before.

#### Scenario: A connector call gets the default budget
- **WHEN** an MCP tool call enters the dispatch path with no operator override set
- **THEN** a budget with a 50-second deadline from entry is readable by every stage the call runs

#### Scenario: The operator overrides the default
- **WHEN** `EXOMEM_MCP_REQUEST_BUDGET_SECONDS` is set to a positive number
- **THEN** new MCP tool calls carry that budget
- **AND** a malformed value falls back to the default with one logged warning

#### Scenario: A client cannot loosen the budget
- **WHEN** a client passes any argument attempting to extend its budget
- **THEN** the tool schema does not accept it and the budget is unchanged

#### Scenario: Local and reconcile-class calls are exempt
- **WHEN** `find` runs from the CLI, or `reconcile` or `maintain_memory` in reconcile mode runs through MCP
- **THEN** no budget applies and no stage is skipped or truncated

### Requirement: Heavy Recall Stages Yield To The Remaining Budget
When a budget applies, the recall path SHALL check the remaining budget against a fixed reserve before starting each heavy stage (`rerank`, the `deep` context pack, `graph_enrich`) and SHALL skip a stage it cannot afford rather than exceed the deadline; the pack assembly SHALL stop adding items at the deadline. Yielding SHALL never reorder the hits that were produced: the ranking equals the unbudgeted call's ranking for the stages that ran. The reranker's reserve SHALL account for a cold model load when the reranker is not resident.

#### Scenario: Rerank is skipped under a tight budget
- **WHEN** `ask_memory` is called with `rerank=true` and the remaining budget is below the rerank reserve
- **THEN** the response is returned without reranking, in the same order the unreranked call produces
- **AND** the response names `rerank` as skipped

#### Scenario: The deep pack is truncated at the deadline
- **WHEN** `ask_memory` is called with `deep=true` and the deadline arrives while the pack is being assembled
- **THEN** the pack returned is a prefix of the pack the unbudgeted call assembles
- **AND** the response names `pack` as truncated

#### Scenario: A cold reranker needs a larger reserve
- **WHEN** the reranker singleton is not resident and `rerank=true` is requested
- **THEN** the reserve applied is the cold-load reserve, and the stage is skipped if the remaining budget is below it

#### Scenario: A call that fits the budget is unchanged
- **WHEN** every requested stage fits inside the remaining budget
- **THEN** the response is identical to the unbudgeted response and carries no budget block

### Requirement: A Budgeted Response Names What It Skipped
When a budget applied and at least one stage was skipped or truncated, the recall response SHALL carry a `budget` block with `applied`, `seconds`, `remaining_ms_at_return`, `skipped` and `truncated`, in compact detail as well as full. When nothing was skipped or truncated the block SHALL be omitted. The block SHALL be advisory: the call is a success, and the agent may re-ask with a narrower option set.

#### Scenario: The block appears only when something was left out
- **WHEN** a budgeted call skipped `graph_enrich`
- **THEN** the compact response carries `budget.skipped == ["graph_enrich"]` and `budget.applied == true`
- **AND** an otherwise identical call that skipped nothing carries no `budget` key

### Requirement: The Budget Bounds Waits, Never Canonical Commits
The request budget SHALL bound only waiting and optional work. A canonical mutation that has begun SHALL complete and persist its terminal regardless of the deadline; the writer lease's derived acknowledgement deadline SHALL be the earlier of its entry-based value and the request deadline minus a fixed delivery reserve, so that a committed write is reported before the client stops listening, with `derived_sync` stating what is still behind.

#### Scenario: A commit under way is never interrupted
- **WHEN** the request deadline arrives while a canonical commit is executing
- **THEN** the commit completes, its terminal is persisted, and the response reports it as committed

#### Scenario: The acknowledgement deadline follows the request deadline
- **WHEN** a mutation enters the writer lease with 20 seconds of request budget remaining
- **THEN** the derived acknowledgement wait ends no later than 15 seconds later and the terminal reports `derived_sync` pending for anything not yet proven
