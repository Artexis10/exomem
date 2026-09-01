## ADDED Requirements

### Requirement: Write-advisory decisions live in their own review-state namespace

The portable review-state store SHALL record decisions about write-path advisories under a dedicated identity namespace that cannot collide with attention, activation, relation, or adoption identities. Dismiss, snooze, and reopen SHALL carry the same semantics as for queue items: a dismissal binds to one exact `(review identity, fingerprint)` pair; snooze expires by date; reopen clears every historical fingerprint for the identity.

Recording a write-advisory decision SHALL NOT create a queue item, alter any queue's ranking, or modify any note.

#### Scenario: Triage accepts a write-advisory reference

- **WHEN** a write-path advisory's review reference is dismissed through the explicit triage surface with a reason
- **THEN** the decision is recorded in the portable review state under the write-advisory namespace
- **AND** no attention, activation, relation, or adoption item is created or modified

#### Scenario: Namespaces stay isolated

- **WHEN** a write-advisory identity and an attention-queue identity are derived from the same underlying page
- **THEN** the two identities are distinct
- **AND** a decision recorded against one has no effect on the other
