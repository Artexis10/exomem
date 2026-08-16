## ADDED Requirements

### Requirement: Bootstrap teaches the epistemic commitments

The portable bootstrap contract SHALL include a dedicated section stating the
commitments that govern how durable knowledge may change over time, expressed as
imperative instructions rather than description.

The section SHALL state that raw captured material is append-only and MUST NOT be
rewritten or deleted, that a changed conclusion SHALL be superseded rather than
overwritten so the earlier view stays readable, that a durable expectation about a
future observation SHALL be written down before its answer is known, that a judgment
SHALL be categorical and MUST NOT be recorded as a number, percentage, or hedge, and
that a refuted claim SHALL keep active standing rather than being treated as
superseded.

The section SHALL state that a genuine conflict between conclusions is recorded as a
typed relation and MUST NOT be silently reconciled away.

#### Scenario: The contract states the commitments imperatively

- **WHEN** a generic client reads the bootstrap contract
- **THEN** it can identify the append-only rule for raw material, the supersession rule
  for changed conclusions, the rule that an expectation is recorded before its outcome,
  the prohibition on numeric confidence, and the rule that a refuted claim stays active
- **AND** it can identify that a genuine conflict is recorded as a typed relation

#### Scenario: Reading the contract changes nothing

- **WHEN** bootstrap returns the epistemic commitments
- **THEN** no page, relation, unit, folder, or migration is created by reading them
- **AND** the response contains no vault content, path, or private project name

### Requirement: Bootstrap teaches the shipped epistemic vocabulary

The portable bootstrap contract SHALL name the governed vocabulary an agent needs to
close a claim: the closed set of epistemic outcomes, the governed unit-metadata keys
that carry a judgment and a revisit date, and the governed unit kinds for a question, a
hypothesis, and a prediction.

The taught outcome set SHALL be exactly the closed set the runtime accepts, and the
taught governed unit-metadata keys SHALL be exactly the keys the runtime parses and
validates. The contract MUST NOT teach an outcome, key, or kind the runtime does not
accept.

The contract SHALL state that the revisit date is one exact ISO calendar date, that it
is a due date rather than an expiry, and that nothing is removed, decayed, or
downranked when it passes.

#### Scenario: The taught vocabulary matches the runtime vocabulary

- **WHEN** a client reads the bootstrap epistemic vocabulary
- **THEN** the outcomes it names are exactly the outcomes the runtime accepts for a
  verdict
- **AND** the governed unit-metadata keys it names are exactly the keys the runtime
  parses
- **AND** the unit kinds it names for a question, a hypothesis, and a prediction are
  governed kinds the runtime recognises

#### Scenario: The revisit date is taught as a due date

- **WHEN** a client reads the guidance for the revisit-date key
- **THEN** it learns the exact calendar-date format
- **AND** it learns that passing the date removes, expires, or downranks nothing

### Requirement: Bootstrap nudges durable expectations into predictions

The portable bootstrap contract SHALL instruct agents that a durable expectation about a
future observation is recorded as a prediction unit carrying a revisit date, rather than
left in prose, left in the assistant's own short-term memory, or recorded as an observed
fact.

The contract's routing guidance SHALL distinguish a claim about a future observation
from observed state and from planning intent, so an expectation is not misrouted into
either.

#### Scenario: An expectation becomes a prediction

- **WHEN** an agent reads the capture guidance and holds a durable expectation about a
  future observation
- **THEN** it learns to record it as a prediction unit with a revisit date
- **AND** it learns that the assistant's own short-term memory is not the place for it

#### Scenario: An expectation is not an observation or a plan

- **WHEN** the contract describes the boundary between observed state and intended
  future state
- **THEN** it also identifies a checkable claim about a future observation as neither

### Requirement: Bootstrap exposes question, hypothesis, and prediction recipes

The bootstrap authoring recipes SHALL include a recipe for recording an open question, a
hypothesis, and a prediction. Each recipe SHALL state that it describes material
authored inside a compiled page and MUST NOT imply that it is a new page type.

The prediction recipe SHALL name the revisit-date key and the verdict key, and SHALL
state that correcting the wording of a prediction preserves its verdict.

#### Scenario: Recipes are present and correctly scoped

- **WHEN** a client reads the bootstrap authoring recipes
- **THEN** it finds a recipe for a question, a hypothesis, and a prediction
- **AND** each states that it is authored inside a compiled page rather than as its own
  page type

### Requirement: The epistemic contract reaches every client tier

The epistemic commitments and vocabulary SHALL be present in the default compact
bootstrap profile, not only in richer profiles.

The commitments SHALL remain present when the active surface exports a reduced set of
commands. Guidance naming a specific command MAY be removed for a surface that cannot
call it, but removing such guidance MUST NOT remove a commitment.

The contract version SHALL move when this section is added.

#### Scenario: Compact carries the doctrine

- **WHEN** a generic client calls bootstrap with default arguments
- **THEN** the response includes the epistemic commitments and the epistemic vocabulary

#### Scenario: A reduced surface keeps the commitments

- **WHEN** bootstrap runs on an active surface that exports almost no commands
- **THEN** the epistemic commitments are still present in full
- **AND** the response names no command that surface cannot call
