## MODIFIED Requirements

### Requirement: Experiment Outcome Is A Closed Categorical Enum
An experiment page MAY carry an `outcome:` frontmatter field whose value is exactly one key of the verdict registry: the shipped entries `abandoned`, `confirmed`, `inconclusive`, `qualified` and `refuted`, plus any verdict the vault's registry adds. It is the same vocabulary as a semantic unit's `verdict`. A frontmatter write that sets `outcome` to any other value SHALL be refused, and a frontmatter write that sets `outcome` on a page that is not an experiment SHALL be refused. The field SHALL remain optional: a concluded experiment without a recorded outcome SHALL NOT be rejected by the write path.

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

#### Scenario: A vault-added verdict is a valid outcome
- **WHEN** the vault's registry adds verdict `retracted`, and a caller patches an experiment page's frontmatter with `outcome` set to `retracted`
- **THEN** the write succeeds and the field is recorded
