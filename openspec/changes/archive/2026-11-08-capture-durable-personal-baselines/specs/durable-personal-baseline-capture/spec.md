## ADDED Requirements

### Requirement: Durable personal baseline eligibility
The active agent SHALL proactively capture ordinary conversational context as a durable personal baseline only when both its stability or recurrence and its reusable value for later comparison, interpretation, or decisions are clear. Eligible classes SHALL include stable preferences, recurring habits or routines, historical quantitative or contextual baselines, and durable affiliations or memberships. Fleeting preferences, one-off activities, incidental associations or subscriptions, isolated mundane trivia, unresolved claims, and tentative claims SHALL remain unwritten.

#### Scenario: Stable preference is eligible
- **WHEN** a person casually states that a preference held historically and still holds now
- **THEN** the active agent treats the preference as eligible durable context without requiring an explicit remember command

#### Scenario: Recurring routine is eligible
- **WHEN** independent context establishes a repeated routine with future interpretive value
- **THEN** the active agent treats the routine as eligible durable context

#### Scenario: Historical comparison baseline is eligible
- **WHEN** a historical reference point will make a later state or measurement meaningfully interpretable
- **THEN** the active agent treats the reference point as eligible durable context

#### Scenario: Durable affiliation is eligible
- **WHEN** ordinary prose establishes an enduring membership, role, or affiliation
- **THEN** the active agent treats that affiliation as eligible durable context

#### Scenario: Casual trivia remains quiet
- **WHEN** prose describes a preference for today, one attendance, a trial subscription, an isolated mundane metric, an incidental association, or an uncertain claim
- **THEN** the active agent performs no durable write for that context

### Requirement: Baseline routing follows semantic ownership
The active agent SHALL attach a stable facet or affiliation to a uniquely resolved Entity, SHALL otherwise capture one concise compiled observation for an eligible baseline, and SHALL use Records only for a compatible observed event or measurement. The agent MUST NOT silently create a collection, schema, or Entity merely because a baseline was mentioned.

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
- **WHEN** no existing collection accepts the event or measurement
- **THEN** the active agent does not silently create a collection or change a schema

### Requirement: Baseline capture remains distinct from adjacent lifecycle classes
Durable personal baseline eligibility SHALL retain both the durability-or-recurrence discriminator and the reusable-comparison discriminator. It SHALL NOT subsume executed-method outcomes, future intent, or arbitrary observed events.

#### Scenario: Removing durability changes classification
- **WHEN** a mutation removes the stability-or-recurrence discriminator
- **THEN** a paired fleeting-preference fixture is misclassified and the contract test fails

#### Scenario: Removing reusable value changes classification
- **WHEN** a mutation removes the reusable-comparison discriminator
- **THEN** a paired mundane-trivia fixture is misclassified and the contract test fails

#### Scenario: Executed method remains separate
- **WHEN** a case concerns a method that was actually executed
- **THEN** its existing capture route still requires a reported informative outcome and a reusable method or lesson

#### Scenario: Intent and observation retain their owners
- **WHEN** a statement is future intent or an observed event rather than durable background context
- **THEN** it follows the Planning or Records contract respectively and does not pass through baseline capture
