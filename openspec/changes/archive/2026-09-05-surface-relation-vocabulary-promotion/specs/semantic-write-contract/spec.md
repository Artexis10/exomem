## ADDED Requirements

### Requirement: Unregistered relation observations are advisory but non-qualifying

When an authored relation row resolves with registry status `unregistered`, the semantic write evaluator SHALL emit an `unregistered_relation` warning rather than an independent blocking error. The unresolved relation fact SHALL remain ineligible for typed-edge and connectivity qualification and SHALL NOT satisfy relation disposition.

Deprecated relations and registry scope violations SHALL remain blocking errors under their existing rules.

#### Scenario: A separate governed connection permits advisory feedback

- **WHEN** a compiled write contains an unregistered relation row and another outbound relation independently satisfies the connectivity lane
- **THEN** the evaluator reports `unregistered_relation` with warning severity
- **AND** relation disposition is satisfied only by the independently qualifying relation
- **AND** the unregistered observation does not block the write

#### Scenario: An unknown-only relation remains blocked by disposition

- **WHEN** the only authored relation row uses an unregistered label
- **THEN** the evaluator reports `unregistered_relation` with warning severity
- **AND** neither typed-edge nor connectivity qualification accepts that fact
- **AND** relation disposition remains unsatisfied and blocks the write

#### Scenario: Governed registry violations remain errors

- **WHEN** a relation resolves as deprecated or outside its permitted scope
- **THEN** its existing registry finding remains an error
