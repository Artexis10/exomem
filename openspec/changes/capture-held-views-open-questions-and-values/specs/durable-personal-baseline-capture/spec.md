## MODIFIED Requirements

### Requirement: Durable personal baseline eligibility
The active agent SHALL proactively capture ordinary conversational context as a durable personal baseline only when both its stability or recurrence and its reusable value for later comparison, interpretation, or decisions are clear. Eligible classes SHALL include stable preferences, recurring habits or routines, historical quantitative or contextual baselines, and durable affiliations or memberships. Fleeting preferences, one-off activities, incidental associations or subscriptions, isolated mundane trivia, unresolved claims, and tentative personal claims SHALL remain unwritten. This requirement governs personal baseline context only: a held interpretation, an open question the work leaves open, and the values a task established follow the stepping-stone capture predicate in `agent-bootstrap-contract`.

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
