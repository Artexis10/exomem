## ADDED Requirements

### Requirement: Entity lifecycle actions retain existing authority classes
Agent-initiated entity-candidate surfacing and curation-plan proposals SHALL be `structural_suggestions` and SHALL only surface when that class disposition permits. An unknown-kind entity-type registry save, Entity creation, and curation apply, resume, or compensation SHALL be `restructure_execution` and SHALL remain exactly confirm-required. Acceptance of a graph relation SHALL be `link_acceptance` and SHALL remain confirm-required; when relation acceptance is a step inside curation apply, the stricter enclosing `restructure_execution` confirmation SHALL govern the whole apply. Candidate detection and work-item reads grant no write authority and this change SHALL add no standing-delegation cell.

These authority classes and confirmation scenarios define the unchanged v1 mode. `activate-agent-led-vocabulary-evolution` SHALL be the sole v2 authority dependency. Only its explicit activation, supported canonical effects, fresh authority checks, and receipt binding may authorize additive steps; lifecycle detection, stored proposals, prominence, and prior confirmations SHALL NOT grant that authority. Non-additive steps retain their existing confirmation requirements.

#### Scenario: Candidate surfacing is advisory only
- **WHEN** a recurring identity qualifies without an explicit user request
- **THEN** the active agent may surface it only under the `structural_suggestions` disposition
- **AND** detection does not create a registry type, Entity, relation, or curation plan

#### Scenario: Unknown kind registration remains confirmed restructuring
- **WHEN** the active agent judges that no registered type fits and proposes a vault type with rationale
- **THEN** the registry save is `restructure_execution` and requires exact in-conversation confirmation plus every existing guarded-save check

#### Scenario: Promotion and hydration apply remain confirmed
- **WHEN** a promotion or hydration curation plan is ready to apply, resume, or compensate
- **THEN** the action is `restructure_execution` and remains confirm-required
- **AND** no prominence or family disposition lifts that ceiling

#### Scenario: Relation acceptance keeps its own confirmation boundary
- **WHEN** the agent proposes an accepted relation outside curation
- **THEN** it uses `link_acceptance` with confirmation
- **AND** the same step inside curation is covered by the stricter confirmed `restructure_execution` apply
