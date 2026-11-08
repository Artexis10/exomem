## MODIFIED Requirements

### Requirement: Bootstrap teaches the epistemic commitments

The portable bootstrap contract SHALL include a dedicated section stating the
commitments that govern how durable knowledge may change over time, expressed as
imperative instructions rather than description.

The section SHALL state that raw captured material is append-only and MUST NOT be
rewritten or deleted, that a changed conclusion SHALL be superseded rather than
overwritten so the earlier view stays readable, that a durable expectation about a
future observation SHALL be written down before its answer is known, that a verdict
SHALL be categorical, that a degree of belief SHALL be recorded as a belief level or,
only together with a holder and a basis, as a probability, and MUST NOT be recorded
as a number without both, and that a refuted claim SHALL keep active standing rather
than being treated as superseded.

The section SHALL state that a genuine conflict between conclusions is recorded as a
typed relation and MUST NOT be silently reconciled away.

#### Scenario: The contract states the commitments imperatively

- **WHEN** a generic client reads the bootstrap contract
- **THEN** it can identify the append-only rule for raw material, the supersession rule
  for changed conclusions, the rule that an expectation is recorded before its outcome,
  the rule that a verdict is categorical, the rule that a belief number needs a holder
  and a basis, and the rule that a refuted claim stays active
- **AND** it can identify that a genuine conflict is recorded as a typed relation

#### Scenario: Reading the contract changes nothing

- **WHEN** bootstrap returns the epistemic commitments
- **THEN** no page, relation, unit, folder, or migration is created by reading them
- **AND** the response contains no vault content, path, or private project name
