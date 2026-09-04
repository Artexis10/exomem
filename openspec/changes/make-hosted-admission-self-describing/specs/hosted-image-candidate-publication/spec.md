## ADDED Requirements

### Requirement: Publishing a runtime candidate registers it with the service catalogue

The system SHALL register a pending agent-contract candidate for a runtime release as
part of publishing that release's signed image candidate, so the deployed runtime and
the control-plane service catalogue cannot diverge silently. Registration SHALL record
the candidate as pending only; it SHALL NOT promote it, make it live, or alter any
existing live cohort.

#### Scenario: A published runtime candidate appears in the catalogue

- **WHEN** a signed runtime image candidate is published for a release
- **THEN** a corresponding agent-contract candidate is registered for that release and
  protocol version in state `pending`
- **AND** no candidate is promoted and no live cohort changes as a result

#### Scenario: Registration is idempotent for a republished release

- **WHEN** publication runs again for a release whose candidate is already registered
- **THEN** no duplicate candidate is created
- **AND** the existing candidate's state is unchanged

#### Scenario: A registration failure does not fail the publication

- **WHEN** candidate registration fails while the signed image candidate has published
  successfully
- **THEN** the image publication remains successful and durable
- **AND** the registration failure is surfaced as its own alert for manual recovery

#### Scenario: Registration authority is scoped to the catalogue

- **WHEN** the publication pipeline registers a candidate
- **THEN** the credential it uses can register candidates only
- **AND** it cannot promote a cohort, alter a live candidate, or modify tenant state
