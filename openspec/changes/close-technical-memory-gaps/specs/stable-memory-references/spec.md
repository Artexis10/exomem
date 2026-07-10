## ADDED Requirements

### Requirement: Persistent governed identity
Every newly created governed Source, Note, Entity, and evidence Markdown sidecar SHALL carry a unique `exomem_id` UUID in frontmatter. Identity SHALL survive content edits, moves, and renames.

#### Scenario: Move preserves canonical identity
- **WHEN** a governed page with an `exomem_id` is moved through Exomem
- **THEN** its `exomem_id` is unchanged and its canonical reference resolves to the new path

### Requirement: Canonical references and compatibility
The system SHALL expose canonical references as `exomem://memory/<uuid>`, accept them anywhere a governed page path is accepted by read, edit, replace, connect, and review operations, and continue accepting existing paths plus `exomem://vault` and `exomem://source` aliases.

#### Scenario: Read by canonical reference
- **WHEN** `read_memory` receives a canonical reference returned by `remember`
- **THEN** it reads the same page as the response's vault-relative path

### Requirement: Rebuildable reference index
The system SHALL maintain a rebuildable ID-to-path SQLite sidecar, detect duplicate or malformed IDs, and fall back to a bounded Markdown rebuild when the sidecar is absent or schema-mismatched.

#### Scenario: Missing sidecar rebuilds from Markdown
- **WHEN** the reference sidecar is removed and a canonical reference is resolved
- **THEN** the system rebuilds the mapping from governed Markdown and resolves the reference without changing note bodies

### Requirement: Explicit ID backfill
`maintain_memory(mode="backfill-ids")` SHALL report proposed changes by default and SHALL write missing IDs only with `dry_run=false`. The write SHALL be atomic, preserve existing IDs, and refuse duplicate-ID ambiguity.

#### Scenario: Dry-run does not mutate the vault
- **WHEN** backfill runs with its default options over legacy pages
- **THEN** it reports pages missing IDs and no vault file changes
