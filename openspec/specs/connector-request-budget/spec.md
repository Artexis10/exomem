# connector-request-budget Specification

## Purpose
Define the request-scoped deadline, optional recall-stage reserves, and observable budget outcomes that keep connector work within the caller's waiting window while preserving canonical writes.

## Requirements

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

### Requirement: Audits Consume Their Request Budget

Budgeted audits SHALL check their remaining allowance before expensive preparation and checks, and between bounded units of work. They SHALL reserve time for final release and delivery. Deadline exhaustion SHALL stop further audit work without an abandoned background scan. Existing local and reconcile exemptions SHALL remain unchanged.

#### Scenario: Corpus preparation exhausts its allowance
- **WHEN** the request allowance expires during corpus preparation
- **THEN** preparation stops at its next bounded checkpoint
- **AND** no incomplete shared corpus or cache is published
- **AND** the report marks corpus-dependent coverage incomplete

#### Scenario: A category exceeds its allowance
- **WHEN** a category exhausts the audit allowance
- **THEN** the audit discards that category's incomplete findings
- **AND** the budget names the interrupted stage and the stages that never started
- **AND** earlier completed checks remain eligible for release

#### Scenario: A local audit has no request budget
- **WHEN** the same audit runs through an existing unbudgeted local or reconcile path
- **THEN** its complete findings, counts and ordering retain their existing meaning

#### Scenario: Interruption crosses a dependency boundary
- **WHEN** an audit expires during a resolver proof or accepted store read batch
- **THEN** the dependency stops cooperatively under the same allowance
- **AND** interruption does not become a cache miss, fallback scan or empty successful check

#### Scenario: A caller expires while sharing a corpus build
- **WHEN** an audit waiter exhausts its allowance during another caller's corpus build
- **THEN** the waiter stops without cancelling the owner's build
- **AND** an expired owner's abandoned build does not pass its request-specific failure to other callers

### Requirement: Audited Result Release Preserves Fresh Authorization

A budgeted audit SHALL obtain fresh authorization for its final report and check its allowance throughout release and accepted read batches. If release cannot complete, it SHALL discard the report and return a fixed `AUDIT_BUDGET_EXHAUSTED` terminal in the existing error shape. This terminal SHALL finalize without corpus enumeration, contain no findings, paths or counts, and recommend narrower categories.

#### Scenario: Final filtering cannot finish
- **WHEN** the delivery allowance expires while the final report is being authorized
- **THEN** no part of that report is released
- **AND** the caller receives the content-free audit budget error

#### Scenario: Authorization changes during an audit
- **WHEN** a page becomes withheld after audit preparation
- **THEN** final release uses the current authorization decision
- **AND** an earlier proof never permits the withheld content to escape

#### Scenario: Other read operations use their existing filter
- **WHEN** a request performs ordinary attention or another read operation
- **THEN** this audit-specific release deadline does not change its existing behavior

#### Scenario: Error delivery has no warm artifact census
- **WHEN** audit release expires while governance is active and the artifact census is cold
- **THEN** the fixed budget terminal finishes without walking the vault or constructing that census
- **AND** a caller-supplied message cannot select this internal terminal path
