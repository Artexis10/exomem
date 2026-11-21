## MODIFIED Requirements

### Requirement: Durable personal baseline eligibility
The active agent SHALL proactively capture ordinary conversational context as a durable personal baseline only when both its stability or recurrence and its reusable value for later comparison, interpretation, or decisions are clear. Eligible classes SHALL include stable preferences, recurring habits or routines, historical quantitative or contextual baselines, and durable affiliations or memberships. A tentative or unresolved claim SHALL NOT become a baseline: the agent SHALL NOT write it as an Entity facet or as an affiliation. When such a claim has reuse value, the agent SHALL save it as tentative, attributed to whoever made it, with its uncertainty, as the capture predicate in `agent-bootstrap-contract` states. Fleeting preferences, one-off activities, incidental associations or subscriptions, and isolated mundane trivia SHALL remain unwritten, because nothing later needs them. This requirement governs personal baseline context only: a held view, an unsure report, an open question the work leaves open, and the values a task established follow the stepping-stone capture predicate in `agent-bootstrap-contract`.

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
- **WHEN** prose describes a preference for today, one attendance, a trial subscription, an isolated mundane metric, or an incidental association
- **THEN** the active agent performs no durable write for that context

#### Scenario: An unsure claim with reuse value is saved as unsure
- **WHEN** a person says they may still belong to a long-running group, the membership is not confirmed, and later work could use the claim
- **THEN** the active agent saves one compiled observation that states the claim as unconfirmed and attributes it to that person
- **AND** the active agent writes no Entity facet, no affiliation and no accepted relation for it
