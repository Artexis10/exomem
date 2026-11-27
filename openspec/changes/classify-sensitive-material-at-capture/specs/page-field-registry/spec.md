## MODIFIED Requirements

### Requirement: Pointer fields are registry data

The governed pointer write SHALL read which frontmatter fields are pointers from the `page-fields` vocabulary registry. That registry is a shipped pack, which the vault overlay may only narrow.
- Each entry SHALL name one frontmatter field and its `role`, a closed attribute whose one value is `pointer`.
- A pointer field names where a page belongs, which file it describes, or which sensitivity classes it carries.
- Code SHALL branch only on the `role` value. Code SHALL NOT branch on an entry key or hold a list of pointer fields.

#### Scenario: The shipped pointer set

- **WHEN** a vault has no overlay
- **THEN** the pointer fields are `project`, `projects`, `domain`, `source_type`, `ingested_into`, `evidence_file`, `data_file`, `artifact_path` and `sensitivity`

#### Scenario: A field outside the registry stays fixed

- **WHEN** a pointer write changes a Source's `captured` field
- **THEN** the write refuses with `APPEND_ONLY` and nothing changes

### Requirement: Only the shipped pack grants a pointer role

The pointer set defines what may change on an append-only page, so widening it is a contract change that ships through review. The shipped pack SHALL be the only source of a `role`.
- A vault overlay or a `schema_memory` save MAY retire a shipped entry by setting its status to deprecated. It SHALL NOT add an entry or change any other value of one.
- No source SHALL declare a key that the write protocol maintains.
- An entry MAY name a `writer`, a closed attribute whose values are `reclassify`, `custody`, `closure` and `classify`. Such a field changes on an append-only page only through that operation.

#### Scenario: Refusal codes

- **WHEN** an overlay row adds an entry, changes a fixed value, or declares `exomem_id`, `moved_from`, `reclassified_from`, `type` or `updated`
- **THEN** it refuses with `PAGE_FIELD_SHIPPED_ONLY`, `PACK_ENTRY_FIXED` or `PAGE_FIELD_PROTOCOL_OWNED`
- **AND** a malformed overlay leaves every shipped entry in force and reports a finding

#### Scenario: An agent widens the pointer set

- **WHEN** an agent proposes and saves a `page-fields` entry that declares `url` with `role: pointer`, then patches a Source's `url`
- **THEN** the save refuses with `PAGE_FIELD_SHIPPED_ONLY` and the Source is unchanged

#### Scenario: An overlay retires a shipped pointer

- **WHEN** the overlay sets the shipped `project` entry's status to deprecated
- **THEN** `project` is no longer a pointer field, and a pointer write that changes it refuses with `APPEND_ONLY`

#### Scenario: An overlay names a protocol key

- **WHEN** the overlay declares `exomem_id` with `role: pointer`
- **THEN** the registry reports a finding, and every shipped entry stays in force

#### Scenario: Only classify writes a Source's sensitivity

- **WHEN** a curation edit step patches `sensitivity` on a Source
- **THEN** it refuses and names the classify operation, and the Source is unchanged
