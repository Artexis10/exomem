## ADDED Requirements

### Requirement: Pointer fields are registry data

The governed pointer write SHALL read which frontmatter fields are pointers
from the `page-fields` vocabulary registry: a shipped pack plus the vault
overlay. Each entry SHALL name one frontmatter field and its `role`, a closed
attribute whose one value is `pointer`. A pointer field names where a page
belongs or which file it describes. The shipped pack SHALL declare `project`,
`projects`, `domain`, `source_type`, `ingested_into`, `evidence_file`,
`data_file` and `artifact_path` as pointers. Code SHALL branch only on the
`role` value and SHALL NOT branch on an entry key or hold a list of pointer
fields.

#### Scenario: An owner declares a vault-specific pointer

- **WHEN** the vault overlay declares a frontmatter field `case_folder` with
  `role: pointer`
- **THEN** a governed pointer write may change that field on a Source, and a
  change to any undeclared field still refuses

### Requirement: A vault overlay only extends shipped pointer fields

The overlay SHALL NOT override, change or retire a shipped entry. A malformed
overlay SHALL leave every shipped entry in force and SHALL report a finding.

#### Scenario: An overlay retires a shipped pointer

- **WHEN** the overlay sets the shipped `project` entry's status to deprecated
- **THEN** the registry reports a finding that names the fixed pack entry
- **AND** `project` stays a pointer field
