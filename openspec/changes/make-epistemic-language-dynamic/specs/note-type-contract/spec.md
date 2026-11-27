## ADDED Requirements

### Requirement: A tried approach has one note with a registry verdict
An agent that ran a test, trial or comparison SHALL write or update one `experiment` note, or one `failure` note for a diagnosed failure, in the same turn. The note SHALL hold the intent, what changed with its commit or run id, a verdict from the verdict registry, the test's limits and what would reopen it. A run record that the project keeps SHALL link to the note and hold no copy of the verdict.

#### Scenario: A change that made the result worse keeps the sign of harm
- **WHEN** a comparison shows that a change made the measured result worse
- **THEN** the note's finding carries the verdict `worsened`, not `refuted`

#### Scenario: No gain maps onto the existing verdicts
- **WHEN** a comparison finds no gain
- **THEN** the verdict is `refuted` where the interval rules out the hoped-for gain, and `inconclusive` where the interval is too wide to say

## RENAMED Requirements

- FROM: `### Requirement: Experiment Outcome Is A Closed Categorical Enum`
- TO: `### Requirement: Experiment Outcome Follows The Verdict Registry`

## MODIFIED Requirements

### Requirement: Experiment Outcome Follows The Verdict Registry
An experiment page MAY carry an `outcome:` frontmatter field whose value is exactly one key of the verdict registry: the shipped entries `abandoned`, `confirmed`, `inconclusive`, `qualified`, `refuted` and `worsened`, plus any verdict the vault's registry adds. It is the same set of keys as a semantic unit's `verdict`; a unit verdict also accepts labels, which resolve to those keys. A frontmatter write that sets `outcome` to any other value SHALL be refused, and a frontmatter write that sets `outcome` on a page that is not an experiment SHALL be refused. The field SHALL remain optional: a concluded experiment without a recorded outcome SHALL NOT be rejected by the write path.

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
- **THEN** they are the same set of keys

#### Scenario: A vault-added verdict is a valid outcome
- **WHEN** the vault's registry adds verdict `retracted`, and a caller patches an experiment page's frontmatter with `outcome` set to `retracted`
- **THEN** the write succeeds and the field is recorded
