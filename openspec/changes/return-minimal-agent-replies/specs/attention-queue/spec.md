## MODIFIED Requirements

### Requirement: Role and transient-state signals share the review lifecycle

The audit SHALL register `artifact_role_promotion` and `transient_state_review` as measurement-only families in default attention and the due-state projection, with bounded page-write delta maintenance and reconciliation. Existing item decisions, family normal/quiet/off dispositions, audience filters, first-surfaced records and carrier caps SHALL apply. Triage SHALL affect presentation without asserting that underlying evidence is resolved. No new tool or mutation-terminal advisory field SHALL be required.

#### Scenario: A committed result reaches an existing carrier
- **WHEN** a committed result creates a valid normal-disposition transient-state candidate
- **THEN** the next eligible bootstrap or recall due-state carrier reports it within the existing count and reference limits, and attention can read its supporting units

#### Scenario: Quiet preserves measurement
- **WHEN** either family's disposition is quiet
- **THEN** audit still measures its findings while default attention and proactive carriers omit that family under the existing disposition contract

#### Scenario: State change clears the finding without a triage record
- **WHEN** a governed correction removes the qualifying current-state conflict
- **THEN** the next valid projection and attention result contain no such finding without requiring a dismissal

### Requirement: Signal families carry a durable disposition

The portable review state SHALL record, per registered signal family, a disposition of `normal`, `quiet`, or `off`, with the reason code, the free-text why, the timestamp, and the origin of the decision. A family SHALL be any registered attention category or write-advisory kind; any other name SHALL be refused. A disposition SHALL persist across engine restarts and across prominence-level changes, and SHALL change only through an explicit decision or a reset to `normal`.

A `quiet` family SHALL be excluded from the default attention union, SHALL contribute nothing to the due-state projection or to any carrier, and SHALL emit no write-path advisory of its kind, while remaining included when its category is requested explicitly, annotated with its disposition. An `off` family SHALL additionally be excluded from explicit category review except under the all-states view, where it is annotated. Exclusion SHALL be applied before cross-queue fusion, so an item flagged only by excluded families is absent and a multi-flagged item keeps its remaining reasons. Audit measurement SHALL be unaffected by any disposition. Per-item decisions and pair stances SHALL compose unchanged with dispositions. A disposition SHALL NOT rewrite a decision: an item whose composed reasons change when a family returns to `normal` SHALL resurface under the existing new-reason rule, with the earlier record still standing under the fingerprint it was recorded against.

#### Scenario: A quiet family leaves the default union and the carriers

- **WHEN** a family is set to `quiet` with a reason, and a page in the vault trips that family and no other
- **THEN** the default attention view does not list the page
- **AND** the due-state block served on the next bootstrap carries no count and no reference for that family
- **AND** an explicit review of that category still lists the page, annotated `quiet`

#### Scenario: An off family is reachable only through the all-states view

- **WHEN** a family is set to `off`
- **THEN** an explicit category review of that family under the open view lists nothing for it
- **AND** the all-states view lists its items annotated `off`
- **AND** the audit still reports the family's findings

#### Scenario: A disposition survives restart and prominence changes

- **WHEN** a family is set to `quiet`, the engine is restarted, and the prominence level is changed to the minimum and then the maximum
- **THEN** the family is still `quiet` with the original reason and timestamp

#### Scenario: Dispositions and item decisions compose

- **WHEN** an item in a `quiet` family is dismissed and the family is later reset to `normal`
- **THEN** the item stays dismissed under its fingerprint
- **AND** a different item in the same family is open

#### Scenario: A multi-flagged item resurfaces under the new-reason rule when a family returns

- **WHEN** an item flagged by two families is dismissed while one of them is `quiet`, and that family is later reset to `normal`
- **THEN** the item is open, because its composed reasons now include one the decision was never taken against
- **AND** the earlier record still stands under the fingerprint it was recorded against

#### Scenario: An unregistered family is refused

- **WHEN** a disposition is requested for a name that is neither a registered attention category nor a write-advisory kind
- **THEN** the decision is refused with a family-specific error and no state changes
