## ADDED Requirements

### Requirement: Entity candidates are selectable active-agent work and never due-state
`entity_recurrence` SHALL remain a registered attention category and SHALL project promotion, hydration, and ambiguity candidates as identity-partitioned review items. It SHALL remain selectable and triageable but SHALL stay outside the unfiltered default union until the existing f21 calibration and acknowledgment gate authorises that migration. Entity candidates MUST NOT enter due-state counters, trigger a background writer, or bypass ordinary review dispositions. Repairing corpus, Entity, relation, or registry state SHALL clear or transition the item on the next pass.

#### Scenario: Explicit entity-candidate attention is bounded
- **WHEN** `attention` is called with `categories=["entity_recurrence"]`
- **THEN** it returns at most one item per identity with its candidate state and bounded evidence
- **AND** one audit pass is reused and no vault content is mutated

#### Scenario: Existing daily queue remains compatible
- **WHEN** `attention` is called without a category filter before f21 acknowledgment
- **THEN** `entity_recurrence` does not displace the existing default queues
- **AND** the category remains discoverable and explicitly selectable

#### Scenario: Candidate never becomes due-state
- **WHEN** promotion, hydration, or ambiguity evidence remains open across mutations or recall
- **THEN** no due-state counter or due-state detail names the candidate
- **AND** no background or server-authored mutation occurs

#### Scenario: Promotion transitions without duplicate rows
- **WHEN** one identity changes from promotion to hydration
- **THEN** attention projects one identity-partitioned row for its current state
- **AND** it does not retain a second stale promotion row
