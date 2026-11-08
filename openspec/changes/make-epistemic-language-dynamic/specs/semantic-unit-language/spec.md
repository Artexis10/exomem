## ADDED Requirements

### Requirement: Kinds and verdicts have one registry source
Unit kinds and verdicts SHALL be vocabulary registry entries with shipped defaults and vault additions. Each SHALL carry a key, labels per language, a dimension and its behaviours. Every component that reasons about kinds or verdicts SHALL read the registry and SHALL NOT hold its own list. The shipped defaults SHALL equal today's kinds and verdicts, and a refuted claim SHALL keep active standing.

#### Scenario: A vault-added kind reaches every reader
- **WHEN** a vault adds kind `procedure` with behaviour `needs_check`
- **THEN** parsing, activation, context packing and upkeep all recognise a `## Procedure` unit and schedule its check

#### Scenario: A vault-added verdict is accepted
- **WHEN** a vault adds verdict `superseded-by-evidence` with behaviour `undercuts`
- **THEN** a unit carrying it parses without error and readers weigh it as undercut

### Requirement: Units carry mood and holder
A unit SHALL accept a governed `mood` whose value is a key in the mood registry (default entries is, should, must, may) and a governed `holder` that names an entity. A unit with a holder SHALL be read as that holder's view. Retrieval and context SHALL NOT merge a unit with mood `should` into a unit with mood `is` about the same subject.

#### Scenario: A norm stays a norm
- **WHEN** one unit says deploys should need two approvals (mood should) and another says deploys need one approval (mood is)
- **THEN** context presents both with their moods, and neither replaces the other

### Requirement: Degree of belief is sourced
A unit SHALL accept a governed `belief` whose value is either a key in the belief-level registry (default entries speculative, plausible, likely, near-certain) or a probability between 0 and 1. A probability SHALL be valid only when the same unit carries a governed `holder` and a governed `basis`. A probability without both SHALL produce a deterministic source-addressed error that names the missing field. A unit with `belief`, `check_by` and a later verdict SHALL be available to calibration scoring.

#### Scenario: A sourced forecast is accepted
- **WHEN** a prediction unit carries `- belief: 0.7`, `- holder: Platform team` and `- basis: last six release cycles`
- **THEN** the unit parses without error and its belief is 0.7 held by the platform team

#### Scenario: An unsourced number is refused
- **WHEN** a claim unit carries `- belief: 0.85` and no holder or basis
- **THEN** the document reports an error that names `holder` and `basis`

#### Scenario: A level needs no source
- **WHEN** a claim unit carries `- belief: likely`
- **THEN** the unit parses without error

## MODIFIED Requirements

### Requirement: Verdict Is A Governed Categorical Unit-Metadata Key
The semantic-unit language SHALL recognize `verdict` as a governed rich unit-metadata key whose value is exactly one key of the verdict registry (default entries `abandoned`, `confirmed`, `inconclusive`, `qualified`, `refuted`) or one of its labels, matched after Unicode NFKC normalization and casefolding. Any other value, including any numeric value, SHALL produce a deterministic source-addressed error whose remediation names the registered verdicts and states that a degree of belief belongs in `belief`, with a holder and a basis when it is a number. The normalized value SHALL be projected onto the parsed unit and included in its serialized form. A verdict SHALL NOT change a unit's lifecycle, standing, or rank.

#### Scenario: A valid verdict is normalized and projected
- **WHEN** a rich unit carries the metadata row `- verdict: Refuted`
- **THEN** the parsed unit's verdict is `refuted` and the document reports no error

#### Scenario: An unknown verdict value is rejected
- **WHEN** a rich unit carries the metadata row `- verdict: probably-wrong`
- **THEN** the document reports an `invalid_rich_verdict` error bound to that source line

#### Scenario: A numeric verdict is refused with the no-confidence reason
- **WHEN** a rich unit carries the metadata row `- verdict: 0.7`
- **THEN** the document reports an `invalid_rich_verdict` error whose remediation states that a confidence is not a verdict and points to `belief` with a holder and a basis

#### Scenario: A refuted unit keeps active standing
- **WHEN** a rich unit on an active page carries `- verdict: refuted`
- **THEN** the unit remains an ordinary active unit and its parent page status is unchanged
