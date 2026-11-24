## ADDED Requirements

### Requirement: Store-mode retirement covers store-owned identifying material

The system SHALL retire a store-mode incarnation's page rows, held content, projection intents and files, and recovery copies through the canonical store owner. Cleanup SHALL redact the incarnation's identifying audit fields and keep their sequencing facts. A managed snapshot or published replica older than the purge revision SHALL keep retirement pending until its owner replaces or retires it.

#### Scenario: Store audit retains the target's earlier paths
- **WHEN** target projection and vector cleanup have finished for an incarnation with exact move lineage
- **THEN** cleanup redacts its identifying audit fields while retaining sequence, revision and operation facts
- **AND** it preserves a replacement incarnation's audit facts at a reused path
- **AND** path fields use non-path opaque tokens, preserving existing null move origins without producing a null path
- **AND** one token per target life and original path preserves replay across both move halves without reuse across targets

#### Scenario: Managed store replica predates retirement
- **WHEN** a managed reusable snapshot or published replica contains state older than the purge revision
- **THEN** retirement remains pending until its owner replaces or retires that copy
- **AND** an unrelated collection transaction sequence cannot substitute for the store revision acknowledgement

### Requirement: Dependency and residue evidence resolve through their producers

A dependency reservation that proves no effect or complete rollback SHALL restore the latch state from before it. Another replica SHALL reconcile an incarnation from canonical custody without a local clear latch. Capture producers SHALL reserve pre-reference residue before their bytes can survive.

#### Scenario: Dependency write fails before publication
- **WHEN** a reserved dependency publication proves no effect or complete rollback
- **THEN** its reservation does not become a permanent invented dependency

#### Scenario: Custody moves to another replica
- **WHEN** canonical Source custody moves through supported replica handoff
- **THEN** the next owner can reconcile that incarnation without relying on an old local clear latch
- **AND** unavailable attempt-local residue ownership remains unknown rather than absent

#### Scenario: Protected staging fails before Source allocation
- **WHEN** a capture leaves owned staging bytes without allocating a Source reference
- **THEN** settlement reports that residue and explicit cleanup continues until its producer acknowledges removal
