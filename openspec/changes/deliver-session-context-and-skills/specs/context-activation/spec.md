## ADDED Requirements

### Requirement: Task leads follow the fused ranking of the discovery lanes

Task discovery SHALL order its leads by the reciprocal-rank fusion of its lanes' rankings for the turn and the focus, at search's adopted fusion constant, with the keyword and dense readings at search's per-lane depth and the method search at the lead limit. After fusion, a routed collection SHALL come first and a compiled note SHALL precede a Source it compiled. A restricted caller's released-corpus lane SHALL keep its own order.

#### Scenario: A busier subject that shares a word does not fill every lead

- **WHEN** more pages than the lead limit repeat one of the turn's words in another sense, and the agent gives no focus
- **AND** the page the turn is about is the dense reading's first page, and the keyword reading ranks it within its candidate depth
- **THEN** the fused ranking places that page ahead of every page that only the keyword reading ranks

#### Scenario: One reading alone keeps its order

- **WHEN** only the keyword reading lists pages for the turn: the dense reading and the method search list none, and the agent gives no focus
- **THEN** the fused ranking follows the keyword reading's order
