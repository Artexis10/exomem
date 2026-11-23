## ADDED Requirements

### Requirement: The fields that carry project keys are registry data

A pointer entry MAY declare `carries`, a closed attribute whose one value is
`project-key`. The shipped pack SHALL declare it on `project` and `projects`.
Like every other attribute, `carries` SHALL come only from the shipped pack.
Code SHALL read these fields from the registry and SHALL NOT hold a list of
project field names.

#### Scenario: A vault overlay gives a field `carries`

- **WHEN** a vault overlay adds `carries: project-key` to a shipped field that
  lacks it
- **THEN** the registry reports `PACK_ENTRY_FIXED` as a finding
- **AND** only `project` and `projects` carry `project-key`

### Requirement: A field that carries project keys takes only registered keys

A value that the governed pointer write or a curation `move` writes to a field
that carries `project-key` SHALL pass the project registry's key check, and a
new key SHALL register through the same write. A relocation SHALL rewrite only
the active fields that carry `project-key`.

#### Scenario: A relocation rewrites only carrying fields

- **WHEN** a relocation moves pages to another project
- **THEN** each page and each cited Source differ only in the fields that carry
  `project-key`, besides the page's path, lineage and rewritten links

### Requirement: A field's value shape is registry data

A field entry MAY declare `shape`, a closed attribute whose one value is
`list`. The shipped pack SHALL declare it on `projects`. Like `carries`,
`shape` SHALL come only from the shipped pack. A writer that gives an absent or
empty field its first value SHALL write a list to a field whose shape is
`list`, and a single value to any other field.

#### Scenario: A relocation fills an empty list field

- **WHEN** a relocation gives a project key to a page whose `projects` field is
  empty and that carries no other project field
- **THEN** the page's `projects` field holds a list of that one key
