## ADDED Requirements

### Requirement: Task leads follow the fused ranking of the discovery lanes

Task discovery SHALL order its leads by the reciprocal-rank fusion of its lanes' rankings, for the turn and for the agent's focus, at search's adopted fusion constant. The keyword and dense readings SHALL read to search's per-lane candidate depth; the method search SHALL keep the lead limit. A collection that the turn routes to SHALL stay first, and a restricted caller's released-corpus lane SHALL keep its own order.

#### Scenario: A busier subject that shares a word does not fill every lead

- **WHEN** more pages than the lead limit, but fewer than the per-lane candidate depth, repeat one of the turn's words in another sense
- **AND** the page the turn is about names that word once and is the dense reading's first page
- **THEN** that page is the first lead, ahead of every page that only the keyword reading ranks

#### Scenario: One reading alone keeps its order

- **WHEN** only the keyword reading lists pages for the turn: the dense reading and the method search list none, and the agent gives no focus
- **THEN** the leads follow the keyword reading's order
