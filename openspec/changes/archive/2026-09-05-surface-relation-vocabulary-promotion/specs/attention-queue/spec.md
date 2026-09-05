## ADDED Requirements

### Requirement: Recurring unregistered relations become proposal candidates

Relation-registry inference SHALL populate its existing proposal scaffold with every unregistered raw relation label observed at or above a recurrence threshold that defaults to three. Each new candidate SHALL preserve the observed label and SHALL leave its core parent and description unset for human choice. An unregistered label below the threshold SHALL remain in the counted evidence but SHALL NOT appear in the proposal.

Inference SHALL remain read-only. It MUST NOT persist a candidate unless the caller separately supplies a reviewed proposal through the existing explicit `save=true` path, and existing expected-hash and observed-relation-deletion guards SHALL remain authoritative.

#### Scenario: Label at threshold is proposed

- **WHEN** deterministic inference observes the same unregistered relation label three times under the default threshold
- **THEN** the proposal scaffold contains that label with unset parent and description
- **AND** no registry file is created or modified

#### Scenario: Label below threshold stays observational

- **WHEN** deterministic inference observes an unregistered relation label fewer than three times
- **THEN** its count and examples remain in the inference response
- **AND** it does not appear in the proposal scaffold

#### Scenario: Proposal requires explicit guarded save

- **WHEN** inference returns one or more recurring candidates without `save=true`
- **THEN** it writes nothing
- **AND** a later save remains subject to the existing reviewed-proposal, expected-hash, and observed-deletion guards
