# vault-scaffold-layout Specification

## Purpose
TBD - created by archiving change move-shipped-schema-out-of-the-note-namespace. Update Purpose after archive.
## Requirements
### Requirement: Product-Owned Markdown Lives Outside The Note Namespace

Exomem SHALL deploy the product-owned governance markdown — the files matched by
`_SHIPPED_SCHEMA_GLOBS` — to `<vault>/.exomem/schema/` rather than to
`<vault>/Knowledge Base/_Schema/`. A dot-directory is used so the content
inherits the same treatment from Obsidian and from any other indexer that every
other non-note directory in the vault already receives, without exomem needing
each of those consumers to be configured.

Per-vault configuration and state SHALL remain in `Knowledge Base/_Schema/`. That
covers `project-keys.yaml`, `relation-registry.yaml`,
`semantic-language-registry.yaml`, `traversal-profiles.yaml`,
`source-taxonomy.yaml`, `contracts/`, `relation-reviews/`, `private-skills/`, and
the activation manifest. These belong to the user, are not markdown, and are not
what pollutes a note index.

#### Scenario: A new vault has no shipped markdown in its note directory

- **WHEN** a vault is initialised
- **THEN** `Knowledge Base/_Schema/SKILL.md` is not created
- **AND** `.exomem/schema/SKILL.md` is created
- **AND** `Knowledge Base/_Schema/project-keys.yaml` is created as before

#### Scenario: Refreshing deploys to the new location

- **WHEN** the shipped schema is refreshed for a vault
- **THEN** the product-owned markdown is written under `.exomem/schema/`
- **AND** no per-vault YAML registry is overwritten

#### Scenario: Only product-owned paths are governed by this layout

- **WHEN** the shipped schema is deployed or migrated
- **THEN** only paths matching `_SHIPPED_SCHEMA_GLOBS` are written or removed
- **AND** any other file under `Knowledge Base/_Schema/` is left exactly as it was

### Requirement: A Vault Is Identified By Either Sentinel

`vault._is_vault` currently returns whether `Knowledge Base/_Schema/SKILL.md`
exists, which makes that file the vault sentinel for `resolve_vault`,
`product_invoke`, `doctor` and the hosted runtime. Exomem SHALL treat a directory
as a vault when **either** the legacy sentinel or the new
`.exomem/schema/SKILL.md` is present, so that vault identity is unchanged both
for a vault that has migrated and for one that has not.

#### Scenario: A legacy vault is still a vault

- **GIVEN** a vault containing `Knowledge Base/_Schema/SKILL.md` and no `.exomem/`
- **WHEN** vault identity is tested
- **THEN** the directory is recognised as a vault

#### Scenario: A migrated vault is still a vault

- **GIVEN** a vault containing `.exomem/schema/SKILL.md` and no
  `Knowledge Base/_Schema/SKILL.md`
- **WHEN** vault identity is tested
- **THEN** the directory is recognised as a vault

#### Scenario: A directory with neither is not a vault

- **GIVEN** a directory containing neither sentinel
- **WHEN** vault identity is tested
- **THEN** the directory is not recognised as a vault

### Requirement: Readers Prefer The New Location And Accept The Legacy One

Every consumer of the shipped governance markdown SHALL resolve the new location
first and fall back to the legacy one, so an existing vault continues to work
with no migration and a migrated vault never reads a stale copy.

#### Scenario: A migrated vault is read from the new location

- **GIVEN** a vault whose shipped markdown is only under `.exomem/schema/`
- **WHEN** a consumer reads the governance contract or its references
- **THEN** the content is served from `.exomem/schema/`

#### Scenario: An unmigrated vault is read from the legacy location

- **GIVEN** a vault whose shipped markdown is only under `Knowledge Base/_Schema/`
- **WHEN** a consumer reads the governance contract or its references
- **THEN** the content is served from `Knowledge Base/_Schema/`

#### Scenario: The new location wins when both are present

- **GIVEN** a vault holding the shipped markdown in both locations
- **WHEN** a consumer reads the governance contract
- **THEN** the content is served from `.exomem/schema/`

### Requirement: Reclaiming The Legacy Copy Is Explicit And Verified

Removing the legacy copy SHALL be an explicit operation, never a side effect of
an upgrade, a refresh, or a read. It SHALL remove a legacy file only after
confirming the new location holds a file with identical bytes, and only for paths
matching `_SHIPPED_SCHEMA_GLOBS`. It SHALL report what it removed and what it
declined to remove.

#### Scenario: Migration removes only verified duplicates

- **GIVEN** a vault holding the shipped markdown in both locations with identical bytes
- **WHEN** the migration runs
- **THEN** the legacy product-owned markdown files are removed
- **AND** the new location still holds every one of them

#### Scenario: A modified legacy file is kept, not deleted

- **GIVEN** a legacy shipped file whose bytes differ from the new location
- **WHEN** the migration runs
- **THEN** that file is not removed
- **AND** the result names it as declined

#### Scenario: An upgrade never removes anything on its own

- **WHEN** a vault is refreshed or read without the migration being requested
- **THEN** no file under `Knowledge Base/_Schema/` is removed

#### Scenario: User content beside the shipped markdown survives

- **GIVEN** a vault with a user-authored file inside `Knowledge Base/_Schema/`
- **WHEN** the migration runs
- **THEN** that file is left in place

### Requirement: The Index Exclusion Moves With The Content

`Knowledge Base/_Schema/` was excluded from exomem's own vault-wide walk
(`VAULT_SCAN_SKIP_DIRS`, reached through `find(scope="vault")`). The new location
SHALL carry that exclusion, in the full walk and in the incremental per-path
predicate alike. Without it the move would relocate the pollution rather than
remove it: the same product-owned markdown would start ranking against real notes
in exomem's own search instead of in the third-party indexer that motivated the
change.

#### Scenario: The vault-wide walk does not yield shipped markdown

- **GIVEN** a vault whose shipped markdown is under `.exomem/schema/`
- **WHEN** the vault-wide markdown walk runs
- **THEN** no path under `.exomem/` is yielded

#### Scenario: The incremental patcher agrees with the full walk

- **WHEN** a path under `.exomem/schema/` is tested against the scan exclusion
- **THEN** it is excluded
- **AND** an ordinary note path is not
