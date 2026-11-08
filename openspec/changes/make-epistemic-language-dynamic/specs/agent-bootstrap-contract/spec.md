## MODIFIED Requirements

### Requirement: Bootstrap teaches the epistemic commitments

The portable bootstrap contract SHALL include a dedicated section stating the
commitments that govern how durable knowledge may change over time, expressed as
imperative instructions rather than description.

The section SHALL state that raw captured material is append-only and MUST NOT be
rewritten or deleted, that a changed conclusion SHALL be superseded rather than
overwritten so the earlier view stays readable, that a durable expectation about a
future observation SHALL be written down before its answer is known, that a verdict
SHALL be categorical, that a degree of belief SHALL be recorded as a belief unit that
names its holder, with a belief level or, only together with a basis, a probability,
and MUST NOT be recorded as a number without a holder and a basis, and that a refuted
claim SHALL keep active standing rather than being treated as superseded.

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

### Requirement: Bootstrap teaches the shipped epistemic vocabulary

The portable bootstrap contract SHALL name the governed vocabulary an agent needs to
close a claim: the shipped verdicts, the governed unit-metadata keys that carry a
judgment, a revisit date and a degree of belief, and the governed unit kinds for a
question, a hypothesis, a prediction and a belief.

The contract SHALL read the taught verdicts and kinds from the shipped vocabulary pack,
so the taught verdicts SHALL be exactly the shipped verdicts. It SHALL state that a
vault may add verdicts and name the schema read that lists them, and it SHALL NOT
include a vault's own entries. The taught governed unit-metadata keys SHALL be exactly
the keys the runtime parses and validates. The contract MUST NOT teach a verdict, key,
or kind the runtime does not accept.

The contract SHALL state that the revisit date is one exact ISO calendar date, that it
is a due date rather than an expiry, and that nothing is removed, decayed, or
downranked when it passes.

#### Scenario: The taught vocabulary matches the runtime vocabulary

- **WHEN** a client reads the bootstrap epistemic vocabulary
- **THEN** the verdicts it names are exactly the shipped verdicts, and the runtime
  accepts each of them
- **AND** the governed unit-metadata keys it names are exactly the keys the runtime
  parses
- **AND** the unit kinds it names for a question, a hypothesis, a prediction and a
  belief are governed kinds the runtime recognises

#### Scenario: A vault-added verdict is discoverable

- **WHEN** the vault's registry adds verdict `retracted` and a client reads bootstrap
- **THEN** the contract does not name `retracted`
- **AND** it names the schema read whose verdict list includes `retracted`

#### Scenario: The revisit date is taught as a due date

- **WHEN** a client reads the guidance for the revisit-date key
- **THEN** it learns the exact calendar-date format
- **AND** it learns that passing the date removes, expires, or downranks nothing
