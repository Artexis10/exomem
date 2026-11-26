## ADDED Requirements

### Requirement: A provenance field may record an item's own time

Besides `envelope` and `member-time`, a provenance entry MAY carry `source: item-time`. That is the created time an item states for itself inside an app export, as the export expansion recorded it. The shipped pack SHALL declare one such entry, `item-created`, with the field `item_created`. Filing SHALL write it only on a Source derived from an export item that stated a readable time. It SHALL leave the field out otherwise, and never guess it. `captured` SHALL keep the filing time.

#### Scenario: A conversation keeps the time it was started

- **WHEN** filing files a Source derived from an export conversation created at 2024-03-05T09:12:00Z
- **THEN** the Source's frontmatter carries `item_created: 2024-03-05T09:12:00Z`, and its `captured` is the filing time

#### Scenario: An ordinary file has no item time

- **WHEN** filing files a staged photo that did not come from an export
- **THEN** its Source carries no `item_created`
