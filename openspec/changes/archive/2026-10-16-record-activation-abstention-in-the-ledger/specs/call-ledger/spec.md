# Spec Delta

## MODIFIED Requirements

### Requirement: Every MCP Call Produces Exactly One Ledger Row

The system SHALL append exactly one ledger row for each completed MCP tool call — read or
write, success or abstention or refusal or error — recorded at the single point where both the call's
duration and its outcome are known. A call SHALL NOT produce zero rows and SHALL NOT produce
more than one row.

#### Scenario: Successful write appends one row

- **WHEN** an MCP write tool call completes normally
- **THEN** exactly one ledger row is appended for that call
- **AND** its `outcome` is `ok`
- **AND** its `request_id` matches the correlation id used by the call tracer

#### Scenario: Read call is recorded too

- **WHEN** an MCP read tool call completes normally without activation abstention
- **THEN** exactly one ledger row is appended with `outcome` of `ok`
- **AND** the row is present even though no mutation and no writer lease were involved

#### Scenario: One call is never double-counted

- **WHEN** a single tool call passes through both the middleware seam and the wrapper seam
- **THEN** only one row is emitted for that call, not one per seam

#### Scenario: A call rejected before the leaf is still recorded

- **WHEN** a call is rejected by a pre-check that runs ahead of the tool leaf — the binary-blob
  content guard, or `edit_memory` operation normalization
- **THEN** exactly one ledger row is appended for that call
- **AND** its `outcome` is `refused` carrying the rejecting check's own code
- **AND** the row records the arguments the client actually sent, before any translation


## ADDED Requirements

### Requirement: Completed Activation Abstention Has A Distinct Outcome

A completed `activate_context` call SHALL record `abstained` when its final served packet has strict `abstained: true`, with a null error code. A refusal SHALL take precedence, and a dispatch or serialization exception SHALL record `error`. Other completed calls SHALL retain `ok`. The system SHALL NOT infer abstention from latency, material size, skipped stages or reason text.

#### Scenario: Budget exhaustion returns an abstention

- **WHEN** request-budget exhaustion produces a final activation packet with strict `abstained: true`
- **THEN** the ledger row records `outcome: abstained` and a null error code
- **AND** it does not report `ok`

#### Scenario: Useful budget-limited activation completes normally

- **WHEN** optional activation work reaches the budget and the final packet has strict `abstained: false`
- **THEN** the ledger row retains `outcome: ok`
- **AND** that outcome does not certify usefulness, completeness or relevance

#### Scenario: Ordinary activation abstention is also observed

- **WHEN** the final packet abstains for unresolved, ambiguous or warming input without budget exhaustion
- **THEN** the ledger row records `outcome: abstained`
- **AND** budget limitation remains a separate observation

#### Scenario: Failure transport retains precedence

- **WHEN** activation returns a refusal envelope or dispatch or serialization raises
- **THEN** the ledger records the existing refusal or error outcome and error code
- **AND** it does not substitute activation abstention for that failure

### Requirement: Activation Observations Are Bounded And Content-Free

Schema version 1 SHALL support an optional `activation_result` block containing strict boolean `abstained` and `budget_limited`, plus `reason`. Reasons SHALL use the fixed protocol: `ambiguous`, `unresolved`, `index_warming`, `unavailable`, `disabled`, or `withheld`; unknown reasons become null. Invalid booleans SHALL leave the block absent. Strict observed abstention SHALL still determine the outcome. No packet content SHALL enter the block.

#### Scenario: The final served packet supplies the observation

- **WHEN** the final postfilter changes an activation packet before serving
- **THEN** the stored observation reflects that final packet
- **AND** it records no material, query, conversation, page path or packet reference

#### Scenario: Malformed metadata does not erase abstention

- **WHEN** strict abstention is observed but budget metadata is invalid or a reason is unknown
- **THEN** the outcome remains `abstained`
- **AND** invalid booleans leave the block absent, while an unknown reason becomes null

#### Scenario: Request observations cannot cross calls

- **WHEN** concurrent, nested or cancelled calls share a client or request identifier
- **THEN** each stored activation observation belongs only to its own call
- **AND** a non-activation call receives no activation observation

#### Scenario: Observation failure leaves the response intact

- **WHEN** optional observation capture or ledger serialization fails
- **THEN** telemetry does not change the tool's response or request deadline

### Requirement: Historical Activation Semantics Remain Unknown

Readers SHALL accept the extended outcome vocabulary without rewriting existing rows or hashes. An activation row without `activation_result` SHALL provide no recorded activation semantics. A timing join MAY accept legacy `ok` with null error when the client observes `ok` or `abstained` without error; it SHALL preserve raw server outcome and unknown server semantics. Current recorded disagreements and failure mismatches SHALL reject the join.

#### Scenario: Old rows remain verifiable

- **WHEN** a reader checks a chain containing historical rows and new activation observations
- **THEN** the complete chain verifies without changing stored bytes or hashes
- **AND** the summary reports observation coverage and unknown results separately from execution outcomes

#### Scenario: Legacy timing joins preserve unknown server semantics

- **WHEN** an activation row lacks the observation key and records `ok` with no error
- **AND** its client observes `ok` or `abstained` with no error
- **THEN** the timing join succeeds without copying client facts into the server observation
- **AND** the stored server outcome stays `ok`, with unknown activation semantics

#### Scenario: Current disagreements cannot use the legacy exception

- **WHEN** a row contains an activation observation that disagrees with its outcome or the client observation
- **THEN** the timing join rejects the inconsistency
- **AND** refusal or error mismatches remain rejected even for historical rows
