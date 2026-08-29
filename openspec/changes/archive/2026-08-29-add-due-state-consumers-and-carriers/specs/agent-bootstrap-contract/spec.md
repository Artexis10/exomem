## ADDED Requirements

### Requirement: Bootstrap serves and teaches the due-state block

The bootstrap payload SHALL carry the due-state block for the requesting audience, subject to the same egress rule as every carrier, and SHALL teach its interpretation in the engagement guidance: counts arrive on ordinary responses; a nonzero count is an invitation to consult the review surface, not an instruction to interrupt; before re-raising any surfaced item the agent consults its fingerprint state; moderate signals are agent judgment and silence is preferable to bureaucracy. Post-write guidance SHALL name only fields the default compact response actually carries.

#### Scenario: A fresh session learns the due state at bootstrap

- **WHEN** a session begins with two predictions past their window and one unfinished experiment visible to the audience
- **THEN** the bootstrap payload's due-state block reports those counts with bounded references
- **AND** the engagement guidance in the same payload states how to act on them

#### Scenario: Guidance never names phantom fields

- **WHEN** the bootstrap payload describes post-write feedback
- **THEN** every named response field is present in the default compact response of the writers it describes
