## MODIFIED Requirements

### Requirement: Provenance fields are registry data

The page-field registry SHALL accept the role `provenance` for a frontmatter field that records where an imported file came from. A provenance entry SHALL carry `source`: `envelope`, `member-time` or `item-time`. An `envelope` entry SHALL name its envelope key. Only the shipped pack SHALL grant the provenance role, and a vault overlay MAY retire a provenance entry and nothing else. Code SHALL branch on the `source` attribute and SHALL NOT branch on a provenance entry's key.

#### Scenario: The shipped pack declares the origin fields

- **WHEN** the registry loads the shipped pack
- **THEN** it holds provenance entries for the origin label, upload device, upload browser, file modification time and item created time, each with its `source`

#### Scenario: An overlay tries to add a provenance field

- **WHEN** a vault overlay declares a new entry with role `provenance`
- **THEN** the registry refuses the entry, and filing writes only the shipped provenance fields

## ADDED Requirements

### Requirement: A provenance field may record an item's own time

An `item-time` provenance field records the created time an item states for itself inside an app export, as the export expansion recorded it. The shipped pack SHALL declare one such entry, `item-created`, with the field `item_created`. Filing SHALL write it only on a Source derived from an export item that stated a readable time, and SHALL leave it out otherwise, never guessing. `captured` SHALL keep the filing time.

#### Scenario: A conversation keeps the time it was started

- **WHEN** filing files a Source derived from an export conversation created at 2024-03-05T09:12:00Z
- **THEN** the Source's frontmatter carries `item_created: 2024-03-05T09:12:00Z`, and its `captured` is the filing time

#### Scenario: An ordinary file has no item time

- **WHEN** filing files a staged photo that did not come from an export
- **THEN** its Source carries no `item_created`
