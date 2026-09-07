## ADDED Requirements

### Requirement: Vocabulary guidance is bounded and independent of commit truth

A committed writer response SHALL be able to carry a compact vocabulary-review advisory with item reference, fingerprint, family, evidence availability and next read action when the active disposition permits. Rich definitions and evidence SHALL be fetched through review context, not copied into every acknowledgement. The advisory SHALL obey a declared bounded size and count, use current-write or bounded projection context, and not initiate a full-vault retrieval or model call.

Failure to compute or persist optional guidance SHALL NOT turn a successful mutation into an error, change its replay identity, or trigger duplicate content writes. The response SHALL distinguish guidance unavailable from no findings when computation was attempted but failed. Durable work reconstruction SHALL remain possible from committed state through bounded review/projection recovery. Replaying a mutation SHALL not create another consideration or spend its notification budget again.

#### Scenario: Advice computation fails after commit

- **WHEN** content commits and vocabulary guidance cannot be produced
- **THEN** the terminal remains committed with typed guidance unavailability and the original receipt identity
- **AND** the client is not instructed to repeat the content mutation under a new identity

#### Scenario: Replayed acknowledgement does not nag again

- **WHEN** the same committed request is reconciled or replayed
- **THEN** it returns the same content outcome without generating a duplicate work item or a fresh unsolicited notification
