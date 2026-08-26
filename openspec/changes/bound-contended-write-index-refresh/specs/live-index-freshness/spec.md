## MODIFIED Requirements

### Requirement: Bounded write-time index maintenance

A component deferral that has already durably recorded path-exact receipts covering the batch SHALL count as batch-report success and SHALL NOT seed a full-component refresh demand. The system SHALL mint a durable full-component refresh demand from a write-time outcome only for a component whose incomplete work has no durable path coverage, and SHALL count both outcomes — covered deferrals accepted and uncovered deferrals escalated — in stable content-free telemetry.

#### Scenario: A warm-up-deferred embedding batch does not seed a whole-vault rebuild

- **WHEN** a batch atomic write's embeddings component defers because the embedding model is warming
- **AND** the warm-up deferral has already recorded durable semantic receipts naming exactly that batch's paths
- **THEN** the batch report treats the embeddings component as succeeded-deferred
- **AND** no full-component refresh receipt is minted for the batch
- **AND** the already-queued semantic replay remains the sole durable demand for those paths

#### Scenario: An uncovered deferral still fails closed

- **WHEN** a component defers without durable path-exact coverage of the batch
- **THEN** the batch report fails and a durable full-component refresh demand is minted
- **AND** the escalation increments its telemetry counter
