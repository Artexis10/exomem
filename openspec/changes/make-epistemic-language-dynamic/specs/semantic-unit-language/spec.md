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
