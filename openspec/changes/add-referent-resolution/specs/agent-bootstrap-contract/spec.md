# agent-bootstrap-contract

## MODIFIED Requirements

### Requirement: Bootstrap teaches referent abstention
Bootstrap and the shipped scaffold SHALL teach agents to name only resolved entities, report the unresolved remainder for partial results, ask on ambiguity, and never guess on unresolved results.

#### Scenario: Partial referent result
- **WHEN** an agent receives partial with unresolved_count one
- **THEN** it names the resolved entity and says one identity remains unresolved
