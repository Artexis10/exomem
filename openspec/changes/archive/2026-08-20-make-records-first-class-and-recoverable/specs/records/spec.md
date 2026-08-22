## ADDED Requirements

### Requirement: Agents infer Records participation from observed state

The agent-facing Records contract SHALL route durable observed events and state to `record_memory` from semantic fit rather than requiring the user to say “save”, “log”, “record”, or “Records”. Covered observations SHALL include measurements, sessions, symptoms, transactions, maintenance events, inventory changes, status history, and other attributable facts. The routing contract SHALL preserve the existing boundary: future intent belongs to Planning, received raw material to Sources, proof-bearing artifacts to Evidence, and conclusions to Notes.

When exactly one existing collection is compatible and the observation is sufficiently identified for that schema, an agent operating under a proactive engagement policy MAY append or update it and SHALL report the mutation. Ambiguous collections, missing required identity/date/provenance, or uncertain ownership SHALL produce one focused clarification. When no compatible collection exists, the agent SHALL propose a collection and SHALL NOT silently create a long-lived schema.

#### Scenario: Measurement is inferred without a magic verb
- **WHEN** a user states a new dated measurement in context without asking to save, log, record, or use Records and exactly one existing collection accepts it
- **THEN** the agent routes the observation through `record_memory`, preserves the observed value without interpretation, and reports the committed mutation

#### Scenario: No collection produces a proposal
- **WHEN** a durable observed event fits Records but no compatible collection exists
- **THEN** the agent uses Records discovery and authoring guidance to propose a collection and does not write the event into a Note, Source, Evidence artifact, or silently invented collection

#### Scenario: Competing collections require one clarification
- **WHEN** two releasable Records collections are equally compatible with the observed event
- **THEN** the agent asks one focused collection-selection question and performs no guessed mutation

#### Scenario: Interpretation remains outside Records
- **WHEN** a Records query supplies values that support a possible conclusion
- **THEN** the Records response remains neutral observed state and any durable conclusion is compiled explicitly into a linked Note
