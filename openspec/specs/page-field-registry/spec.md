# page-field-registry Specification

## Purpose
Name which frontmatter fields on a page are pointers, so that a governed write may correct them on an append-only page, and state that only the shipped pack grants that role.

## Requirements

### Requirement: Pointer fields are registry data

The governed pointer write SHALL read which frontmatter fields are pointers
from the `page-fields` vocabulary registry: a shipped pack that the vault
overlay may only narrow. Each entry SHALL name one frontmatter field and its `role`, a closed
attribute whose one value is `pointer`. A pointer field names where a page
belongs or which file it describes. The shipped pack SHALL declare `project`,
`projects`, `domain`, `source_type`, `ingested_into`, `evidence_file`,
`data_file` and `artifact_path` as pointers. Code SHALL branch only on the
`role` value and SHALL NOT branch on an entry key or hold a list of pointer
fields.

#### Scenario: A field outside the registry stays fixed

- **WHEN** a pointer write changes a Source's `captured` field
- **THEN** the write refuses with `APPEND_ONLY` and nothing changes

### Requirement: Only the shipped pack grants a pointer role

The pointer set defines what may change on an append-only page, so widening it
is a contract change that ships through review. The shipped pack SHALL be the
only source of a `role`. A vault overlay or a `schema_memory` save MAY retire
a shipped entry by setting its status to deprecated, and SHALL NOT add an entry
or change any other value of one; a row that would SHALL refuse with
`PAGE_FIELD_SHIPPED_ONLY` or `PACK_ENTRY_FIXED`. A malformed overlay SHALL
leave every shipped entry in force and SHALL report a finding. No source SHALL
declare a key that the write protocol maintains: `exomem_id`, `moved_from`,
`reclassified_from`, `type` or `updated`; such an entry SHALL refuse with
`PAGE_FIELD_PROTOCOL_OWNED`.

An entry MAY name a `writer`, a closed attribute whose values are
`reclassify`, `custody` and `closure`. Such a field changes on an append-only
page only through that operation.

#### Scenario: An agent widens the pointer set

- **WHEN** an agent proposes and saves a `page-fields` entry that declares
  `url` with `role: pointer`, then patches a Source's `url`
- **THEN** the save refuses with `PAGE_FIELD_SHIPPED_ONLY` and the Source is
  unchanged

#### Scenario: An overlay retires a shipped pointer

- **WHEN** the overlay sets the shipped `project` entry's status to deprecated
- **THEN** `project` is no longer a pointer field, and a pointer write that
  changes it refuses with `APPEND_ONLY`

#### Scenario: An overlay names a protocol key

- **WHEN** the overlay declares `exomem_id` with `role: pointer`
- **THEN** the registry reports a finding, and every shipped entry stays in
  force

### Requirement: Provenance fields are registry data

The page-field registry SHALL accept the role `provenance` for a frontmatter field that records where an imported file came from. A provenance entry SHALL carry `source`, either `envelope` or `member-time`, and an `envelope` entry SHALL name its envelope key. Only the shipped pack SHALL grant the provenance role, and a vault overlay MAY retire a provenance entry and nothing else. Code SHALL branch on the `source` attribute and SHALL NOT branch on a provenance entry's key.

#### Scenario: The shipped pack declares the origin fields

- **WHEN** the registry loads the shipped pack
- **THEN** it holds provenance entries for the origin label, upload device, upload browser and file modification time, each with its `source`

#### Scenario: An overlay tries to add a provenance field

- **WHEN** a vault overlay declares a new entry with role `provenance`
- **THEN** the registry refuses the entry, and filing writes only the shipped provenance fields
