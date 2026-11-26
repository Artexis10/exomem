## ADDED Requirements

### Requirement: Provenance fields are registry data

The page-field registry SHALL accept the role `provenance` for a frontmatter field that records where an imported file came from. A provenance entry SHALL carry `source`, either `envelope` or `member-time`, and an `envelope` entry SHALL name its envelope key. Only the shipped pack SHALL grant the provenance role, and a vault overlay MAY retire a provenance entry and nothing else. Code SHALL branch on the `source` attribute and SHALL NOT branch on a provenance entry's key.

#### Scenario: The shipped pack declares the origin fields

- **WHEN** the registry loads the shipped pack
- **THEN** it holds provenance entries for the origin label, upload device, upload browser and file modification time, each with its `source`

#### Scenario: An overlay tries to add a provenance field

- **WHEN** a vault overlay declares a new entry with role `provenance`
- **THEN** the registry refuses the entry, and filing writes only the shipped provenance fields
