## ADDED Requirements

### Requirement: Entity lifecycle actions follow the delegation-envelope classes
Agent-initiated entity-candidate surfacing and curation-plan proposals SHALL be `structural_suggestions` and SHALL only surface when that class disposition permits. Entity merge, supersession or deletion, and curation apply, resume, or compensation SHALL be `restructure_execution` and SHALL remain exactly confirm-required. On a personal vault, an owner's entity-type registry save, including an unknown kind, SHALL follow `proactive_capture` as the `delegation-envelope` ceiling requirement states (owner decision O2, 2026-10-10, in `promote-referents-without-nudges`). On a personal vault, additive Entity creation through the entity writer's resolve-before-create path SHALL follow `proactive_capture` as the `delegation-envelope` ceiling requirement states; a promotion or hydration curation plan remains a confirmed apply. Acceptance of a relation that the server's relation queue suggested SHALL be `link_acceptance` and SHALL remain confirm-required, while a typed edge that the owner's agent authors SHALL follow `proactive_capture` as the `delegation-envelope` ceiling requirement states; when relation acceptance is a step inside curation apply, the stricter enclosing `restructure_execution` confirmation SHALL govern the whole apply. Candidate detection and work-item reads grant no write authority and this change SHALL add no standing-delegation cell.

These authority classes and confirmation scenarios define the v1 mode. `activate-agent-led-vocabulary-evolution` SHALL be the sole v2 authority dependency. Only its explicit activation, supported canonical effects, fresh authority checks, and receipt binding may authorize additive steps; lifecycle detection, stored proposals, prominence, and prior confirmations SHALL NOT grant that authority. Non-additive steps retain their existing confirmation requirements.

#### Scenario: Candidate surfacing is advisory only
- **WHEN** a recurring identity qualifies without an explicit user request
- **THEN** the active agent may surface it only under the `structural_suggestions` disposition
- **AND** detection does not create a registry type, Entity, relation, or curation plan

#### Scenario: Unknown kind registration follows proactive capture for the owner
- **WHEN** the owner's active agent judges that no registered type fits and saves a vault type with rationale
- **THEN** the registry save follows `proactive_capture` with every existing guarded-save check and no in-conversation confirmation
- **AND** a resolved non-owner's save of the same type stays a pending item for the owner

#### Scenario: Promotion and hydration apply remain confirmed
- **WHEN** a promotion or hydration curation plan is ready to apply, resume, or compensate
- **THEN** the action is `restructure_execution` and remains confirm-required
- **AND** no prominence or family disposition lifts that ceiling

#### Scenario: Relation acceptance keeps its own confirmation boundary
- **WHEN** the agent accepts a relation that the relation queue suggested, outside curation
- **THEN** it uses `link_acceptance` with confirmation
- **AND** the same step inside curation is covered by the stricter confirmed `restructure_execution` apply
