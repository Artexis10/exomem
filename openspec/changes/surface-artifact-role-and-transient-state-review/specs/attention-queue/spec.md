## ADDED Requirements

### Requirement: Role and transient-state signals share the review lifecycle

The audit SHALL register `artifact_role_promotion` and `transient_state_review` as measurement-only families in default attention and the due-state projection, with bounded page-write delta maintenance and reconciliation. Existing item decisions, family normal/quiet/off dispositions, audience filters, first-surfaced records and carrier caps SHALL apply. Triage SHALL affect presentation without asserting that underlying evidence is resolved. No new tool or mutation-terminal advisory field SHALL be required.

#### Scenario: A committed result reaches an existing carrier
- **WHEN** a committed result creates a valid normal-disposition transient-state candidate
- **THEN** the next eligible mutation, bootstrap or recall due-state carrier reports it within the existing count and reference limits, and attention can read its supporting units

#### Scenario: Quiet preserves measurement
- **WHEN** either family's disposition is quiet
- **THEN** audit still measures its findings while default attention and proactive carriers omit that family under the existing disposition contract

#### Scenario: State change clears the finding without a triage record
- **WHEN** a governed correction removes the qualifying current-state conflict
- **THEN** the next valid projection and attention result contain no such finding without requiring a dismissal
