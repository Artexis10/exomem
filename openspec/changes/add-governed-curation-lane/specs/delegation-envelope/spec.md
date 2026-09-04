## ADDED Requirements

### Requirement: Curation proposals and execution occupy different authority classes

Agent-initiated curation context and proposal surfacing SHALL be governed by the
existing `structural_suggestions` disposition. Forward-plan apply, resume, and
compensation execution SHALL be governed by the existing
`restructure_execution` class and SHALL remain confirm-required regardless of
prominence, family disposition, repeated prior approvals, or stored plan state.
The served bootstrap and curation workflow contract SHALL teach this split and
require explicit in-conversation user confirmation before the first apply of
each immutable forward or compensation plan.

#### Scenario: Structural suggestions are off

- **WHEN** `structural_suggestions` is off and the user has not explicitly
  requested a curation review
- **THEN** the agent does not proactively surface or propose curation work, while
  an explicit user request remains available

#### Scenario: Plan was proposed at maximal prominence

- **WHEN** a valid curation plan exists while prominence is maximal
- **THEN** the agent still obtains explicit confirmation before apply and no
  setting turns the plan into silent execution

#### Scenario: Compensation follows a previously approved plan

- **WHEN** the user already approved the forward plan and a compensation plan is
  later derived
- **THEN** the compensation plan requires its own explicit confirmation because
  it is a new immutable `restructure_execution` action
