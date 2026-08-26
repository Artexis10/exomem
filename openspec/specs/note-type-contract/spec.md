# note-type-contract Specification

## Purpose
TBD - created by archiving change add-epistemic-loop-primitives. Update Purpose after archive.
## Requirements
### Requirement: Experiments Carry A Concluded Lifecycle Status
The experiment page type's status enum SHALL be `active`, `draft`, `archived`, or `concluded`. `concluded` SHALL mean the experiment finished and its result stands; it SHALL NOT imply archival, and archival SHALL remain the separate act of stepping a page out of active rotation. A status outside that enum SHALL still be refused for an experiment.

#### Scenario: A concluded experiment is accepted
- **WHEN** an experiment page is created with `status: concluded`
- **THEN** creation succeeds without a status validation error

#### Scenario: An invalid experiment status is still refused
- **WHEN** an experiment page is created with `status: finished`
- **THEN** creation fails with a status validation error naming the accepted values

#### Scenario: Concluded is not archived
- **WHEN** an experiment page carries `status: concluded`
- **THEN** the page is not treated as archived

### Requirement: Experiment Outcome Is A Closed Categorical Enum
An experiment page MAY carry an `outcome:` frontmatter field whose value is exactly one of `abandoned`, `confirmed`, `inconclusive`, `qualified`, or `refuted` — the same vocabulary as a semantic unit's `verdict`. A frontmatter write that sets `outcome` to any other value SHALL be refused, and a frontmatter write that sets `outcome` on a page that is not an experiment SHALL be refused. The field SHALL remain optional: a concluded experiment without a recorded outcome SHALL NOT be rejected by the write path.

#### Scenario: A valid outcome is accepted
- **WHEN** a caller patches an experiment page's frontmatter with `outcome` set to `refuted`
- **THEN** the write succeeds and the field is recorded

#### Scenario: An invalid outcome is refused
- **WHEN** a caller patches an experiment page's frontmatter with `outcome` set to `mostly-right`
- **THEN** the write is refused with an error naming the accepted values

#### Scenario: Every frontmatter-write boundary enforces the enum
- **WHEN** a caller creates a page with frontmatter carrying `outcome` set to a numeric value
- **THEN** the write is refused, exactly as the field-patch route refuses it

#### Scenario: Outcome belongs only to experiments
- **WHEN** a caller patches an insight page's frontmatter with `outcome` set to `confirmed`
- **THEN** the write is refused because the field is experiment-only

#### Scenario: Outcome shares the unit verdict vocabulary
- **WHEN** the accepted experiment outcome values and the accepted unit verdict values are compared
- **THEN** they are the same set

### Requirement: Confidence Remains A Non-Field
The frontmatter contract SHALL continue to exclude any numeric confidence, credence, probability, or certainty field, and SHALL state that the categorical `outcome` and `verdict` values are lifecycle state rather than a stored score. A frontmatter write that sets a confidence field SHALL be refused.

#### Scenario: The shipped frontmatter spec names confidence a non-field
- **WHEN** the shipped frontmatter reference is read
- **THEN** it lists `confidence` among the deliberate non-fields and states that `outcome` and `verdict` are categorical state rather than a score

#### Scenario: A confidence frontmatter write is refused
- **WHEN** a caller patches a page's frontmatter with a `confidence` field
- **THEN** the write is refused
