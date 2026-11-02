# stable-memory-references Specification

## Purpose
Give governed human-owned artifacts durable identities and canonical references while keeping paths compatible and all indexes rebuildable from Markdown.

## Requirements

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

### Requirement: Disclosure-neutral reference resolution

Resolution of memory references for a governed read SHALL return, for each requested reference, every canonical page that holds it, duplicates included, before the read applies any authorization. Resolution SHALL choose its source from vault-wide state alone: a current in-process identity projection when the server holds one, otherwise one canonical scan of the Knowledge Base for the whole batch. The choice SHALL NOT depend on which references were asked for, which of them the vault holds, or who may read the pages that hold them. Resolution MAY read an identity projection that is already current, and SHALL create, rebuild, or refresh no index, cache, or sidecar.

Both sources SHALL apply one page membership rule, so the source never changes the answer. A page belongs when it is Markdown by a case-insensitive `.md` suffix, lies under the Knowledge Base outside the directories that ordinary scans exclude, and carries a well-formed `exomem_id`. Line endings SHALL NOT change whether a page's `exomem_id` is read. A missing or malformed `exomem_id` maps to no page. When the Knowledge Base root holds a directory that the identity census never admits but ordinary scans read, such as `trash/`, resolution SHALL take the scan, so that pages in that directory resolve as the scan resolves them.

A reference that the vault does not hold and a reference that only pages hidden from the reader hold SHALL produce identical responses. This requirement bounds the resolution work only. When the read that follows resolution refuses a restricted caller, its completion time SHALL follow the `get-payload-shape` requirement "Restricted direct-read refusals complete at a fixed floor".

#### Scenario: A hidden twin does not shadow a visible page
- **WHEN** one page that the reader may see and one page that the reader may not see both hold the same reference, and the server holds a current identity projection
- **THEN** resolution returns both pages, authorization removes the hidden page, and the read returns the visible page exactly as it does through the scan
- **AND** a reference that two pages visible to the reader both hold reads as ambiguous through either source

#### Scenario: An absent reference and a hidden-only reference read the same
- **WHEN** one vault holds a reference only in a page that the reader may not see, and an otherwise identical vault does not hold that reference
- **THEN** a read of the reference returns the same not-found response in both vaults
- **AND** both resolutions use the same source and perform the same scan count

#### Scenario: A projection that is not current falls back to the scan
- **WHEN** an edit made outside the server is pending, or the server holds no current identity projection
- **THEN** resolution performs one canonical scan for the whole batch and answers from that scan
- **AND** resolution creates or refreshes no projection, index, cache, or sidecar

#### Scenario: A vault with a trash directory resolves through the scan
- **WHEN** the Knowledge Base root holds a `trash/` directory and the server holds a current identity projection
- **THEN** resolution performs one canonical scan for the whole batch
- **AND** a reference that a page in `trash/` holds resolves to that page, as it does through the scan
