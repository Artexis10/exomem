## ADDED Requirements

### Requirement: Transactional Batches Prepare Safe Derived Custody

An acknowledgement-optimized transactional batch SHALL prepare exact derived
custody before its first canonical replacement. Receipt preparation failure
MUST leave the canonical batch untouched. A caught canonical failure SHALL roll
back canonical files as before; failure to clean a now-inapplicable prepared
receipt MUST remain safe because exact before/after proof prevents it from
publishing. A process death at any point SHALL resolve to one of: uncommitted
receipt, exact committed receipt eligible for derived work, explicitly
superseded receipt, or fail-closed reconciliation demand. It MUST NOT create an
accepted-but-unverified derived state.

#### Scenario: Receipt preparation fails

- **WHEN** the derived receipt store cannot durably prepare the intended batch
- **THEN** no canonical destination is replaced
- **AND** the mutation returns a pre-commit failure

#### Scenario: Cleanup fails after canonical rollback

- **WHEN** the canonical batch rolls back successfully but receipt cleanup fails
- **THEN** canonical files retain their complete before-state
- **AND** the stale prepared receipt cannot pass its after-state proof or publish derived rows

#### Scenario: Crash leaves mixed canonical state

- **WHEN** an abnormal process death leaves a state that proves neither the complete before-state nor the complete intended after-state
- **THEN** the receipt fails closed into transactional reconciliation
- **AND** no component publishes a partial generation or replays the canonical leaf

