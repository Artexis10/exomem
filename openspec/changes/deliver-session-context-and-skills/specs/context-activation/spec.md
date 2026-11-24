## ADDED Requirements

### Requirement: Task leads follow the fused ranking of the discovery lanes

Task discovery SHALL order its leads by the reciprocal-rank fusion of its lanes' rankings: the keyword reading, the dense reading and the method search, for the turn and for the agent's focus. It SHALL use the fusion constant and the per-lane candidate depth that search uses, and SHALL NOT order leads by the order in which the lanes ran. A collection that the turn routes to SHALL stay first. A restricted caller's released-corpus lane SHALL keep its own order.

#### Scenario: A busier subject that shares a word does not fill every lead

- **WHEN** more pages than the lead limit repeat one of the turn's words in another sense
- **AND** the page the turn is about names that word once and is the dense reading's first page
- **THEN** that page is the first lead, ahead of every page that only the keyword reading ranks

#### Scenario: One reading alone keeps its order

- **WHEN** the dense reading lists no page for the turn, or the install runs without embeddings
- **THEN** the leads follow the keyword reading's order
