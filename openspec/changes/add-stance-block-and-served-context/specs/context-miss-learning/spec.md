## MODIFIED Requirements

### Requirement: A miss remains linked to its evidence

A source-backed correction SHALL NOT require an incident record. When the user asks to record or learn from the miss, a workflow skill engages this workflow, or the live engagement policy delegates filing an observed outcome, the agent workflow SHALL link the observed miss, source-backed expectation, available context, correction, write receipts, and verification evidence in an existing incident collection. When the response that preceded the miss carried a `served_context_id` (served-context-record), the workflow SHALL link its composed response metadata by that id instead of copying its refs. The row SHALL NOT prove client receipt or use. Missing evidence SHALL remain explicit; report absent or independently established expiry as a gap. Collection manifests SHALL own field names and lifecycle values.

#### Scenario: The user asks only for the fix

- **WHEN** the user reports a wrong answer and asks only that the notes be fixed
- **THEN** the agent makes the source-backed correction with its reason, and files no incident unless the live engagement policy delegates it

#### Scenario: The user asks to record the miss

- **WHEN** the user asks to record the miss and an existing incident collection fits
- **THEN** the agent maps the evidence onto that collection's declared fields and records what it did not verify

#### Scenario: A failure lacks its original delivered context

- **WHEN** an incident preserves the request and source but lacks the delivered context
- **THEN** the workflow records that gap and does not claim to have located the first failed boundary

#### Scenario: A collection uses its own schema

- **WHEN** an existing incident collection declares different field names or lifecycle values
- **THEN** the agent follows that schema and reports missing fields instead of introducing product-defined values

#### Scenario: An incident links its response metadata by row id

- **WHEN** the user asks to record a miss that followed a response carrying a `served_context_id`
- **THEN** the incident links that id as composed response metadata; owner lookup resolves the retained row or reports the evidence gap without inferring client receipt
