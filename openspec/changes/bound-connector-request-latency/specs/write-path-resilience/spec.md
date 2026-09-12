## MODIFIED Requirements

### Requirement: Mutation Delivery Has A 60-Second Single-Origin Budget

The reference Cloudflare HA edge SHALL give mutation-capable MCP tool calls a default 60-second origin budget. Timeout, cancellation, or an origin 5xx MUST NOT replay a mutation-capable request to another replica. Raising the budget MUST NOT substitute for the corpus-context performance fix or change the meaning of acknowledgement loss. The origin SHALL budget its own work below that edge budget: every MCP tool call carries a request-scoped deadline (default 50 seconds, operator-overridable through `EXOMEM_MCP_REQUEST_BUDGET_SECONDS`), the writer lease's derived acknowledgement deadline SHALL derive from it, and the documented origin budget SHALL be below both edge and client timeouts. Documentation SHALL distinguish the reference worker's timeout from the direct tunnel's cap and recommend placing a configurable edge timeout below the client's timeout.

#### Scenario: Origin outlives the edge budget
- **WHEN** the selected origin does not acknowledge a mutation within 60 seconds
- **THEN** the edge returns an acknowledgement-delivery failure and sends no copy to the passive origin
- **AND** the origin may still finish and persist the terminal result

#### Scenario: Live deployment pins the old setting
- **WHEN** a deployed worker variable overrides the code default with the previous 15-second value
- **THEN** the code default is not effective and the rollout remains incomplete
- **AND** the operator must update the live value to 60000 milliseconds rather than assuming a code deploy changed it

#### Scenario: The origin reports before the edge gives up
- **WHEN** a mutation's derived acknowledgement would outlast the request deadline
- **THEN** the origin reports the committed terminal with `derived_sync` pending before the request deadline
- **AND** the edge never reaches its 60-second failure for that call
