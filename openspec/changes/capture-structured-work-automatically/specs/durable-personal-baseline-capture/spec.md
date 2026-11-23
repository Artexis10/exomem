## MODIFIED Requirements

### Requirement: Baseline routing follows semantic ownership
The active agent SHALL attach a stable facet or affiliation to a uniquely resolved Entity, SHALL otherwise capture one concise compiled observation for an eligible baseline, and SHALL send a recurring named item or measurement to Records, reusing a matching collection or creating a minimal one under the bounds that the `records` capability defines. The agent MUST NOT silently create a collection, schema, or Entity merely because a baseline was mentioned.

#### Scenario: Known Entity receives an affiliation facet
- **WHEN** an eligible durable affiliation belongs to one uniquely resolved Entity
- **THEN** the active agent proposes or performs the governed Entity observation or update instead of creating a disconnected duplicate note

#### Scenario: Eligible baseline has no unique Entity route
- **WHEN** a durable baseline is eligible but no unique Entity owns the fact
- **THEN** the active agent captures one concise compiled observation

#### Scenario: Compatible current measurement uses Records
- **WHEN** the conversation contains an observed measurement accepted by an existing compatible Records collection and a historical comparator
- **THEN** the current measurement follows the Records contract while the comparator does not force an unrelated Record or schema change

#### Scenario: Missing compatible collection does not expand schema
- **WHEN** no existing collection accepts the event or measurement and the conversation shows no recurring named item or measurement
- **THEN** the active agent creates no collection and changes no schema

#### Scenario: A recurring measurement without a collection follows the creation rule
- **WHEN** a recurring measurement fits Records and no existing collection accepts it
- **THEN** the active agent follows the Records creation rule under the served `proactive_capture` disposition, and creates no collection outside it
