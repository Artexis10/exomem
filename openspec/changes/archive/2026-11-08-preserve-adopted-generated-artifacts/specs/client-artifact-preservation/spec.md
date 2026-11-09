## ADDED Requirements

### Requirement: Client artifact preservation supports exact selected-artifact adoption
`preserve_artifacts` SHALL accept an optional validated single-artifact adoption envelope while preserving existing non-adoption behavior. The envelope SHALL name one supplied `file_id`, an explicit trigger, and a scoped durable adoption key. Only the selected artifact SHALL commit, and its artifact and versioned companion receipt SHALL publish atomically through the existing append-only Evidence path. The stable response SHALL distinguish committed preservation, transport replay, durable replay, unverifiable replay, failure, and non-committing handoff.

#### Scenario: Existing calls remain compatible
- **WHEN** an existing client calls `preserve_artifacts` without an adoption envelope
- **THEN** its direct-handle staging, multi-file outcomes, append-only collision behavior, and receipt fields remain unchanged

#### Scenario: Adoption commits only the selected handle
- **WHEN** the envelope selects one of several supplied handles
- **THEN** only that handle is staged for canonical commit and no sibling artifact or companion is written

#### Scenario: Companion carries portable receipt identity
- **WHEN** selected bytes commit
- **THEN** their existing Evidence companion contains a versioned adoption block bound to the lane, destination, and committed byte identity and the response projects the same committed receipt

#### Scenario: Handoff token is not preservation
- **WHEN** a client receives only a transfer capability or prepared handoff
- **THEN** the response cannot mark the artifact committed, stored, saved, uploaded, adopted, or delivered

#### Scenario: Textual degradation is forbidden
- **WHEN** exact selected bytes cannot be obtained
- **THEN** the operation fails or requests handoff instead of silently writing a textual reconstruction or another variant
