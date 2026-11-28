## ADDED Requirements

### Requirement: Readable Cross-Page Unit Replacements Retire Current Context

For callers with unrestricted source access, stance, domain profiles and recorded positions SHALL omit a unit that an exact cross-page relation with replacing behaviour supersedes. The original unit SHALL remain readable through its existing reference. If the lookup for admitted claims is incomplete, the affected projection SHALL be unavailable. Shared projections SHALL remain independent of these cross-page edges.

#### Scenario: The replacement comes from outside the current-context set

- **WHEN** a later inspection replaces one unit in the standing equipment summary through an exact typed link
- **THEN** activation omits that unit from the current lines
- **AND** another unit beside the predecessor remains current
- **AND** the original unit remains readable at its reference

#### Scenario: A newer unlinked claim is available

- **WHEN** a newer inspection supplies similar text without an exact replacement relation
- **THEN** the newer inspection alone does not retire the older unit

#### Scenario: The successor is withheld

- **WHEN** a caller can read the predecessor but cannot read the cross-page successor
- **THEN** the predecessor's current-context state, counts and public metadata agree with the state in which the successor is absent
