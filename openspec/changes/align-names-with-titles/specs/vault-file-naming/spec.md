## MODIFIED Requirements

### Requirement: No Existing File Is Renamed

A write and a change of filename style SHALL NOT rename, move or rewrite any file already on disk, and SHALL NOT alter any wikilink already written, so no link that resolves before the change stops resolving after it. A retitle SHALL leave the file name as it is. The runtime SHALL report a page whose name no longer follows its title, as the `name_drift` review item and the `inspect_name_drift` listing, and SHALL leave the rename to a curation relocation that the user approved.

#### Scenario: Switching style leaves the vault untouched

- **GIVEN** a vault of existing notes written under `slug` style
- **WHEN** the vault is reconfigured to `title` style
- **THEN** no existing file is renamed and no existing link target is rewritten
- **AND** subsequently written notes use the new style
- **AND** the `name_drift` item lists the notes whose names differ from their titles' names

#### Scenario: A retitle keeps the name and suggests the rename

- **GIVEN** a page named for its title
- **WHEN** its title changes
- **THEN** the file keeps its name
- **AND** the write response suggests the rename

#### Scenario: A mixed vault resolves links under both styles

- **GIVEN** a vault containing notes named under both styles
- **WHEN** a wikilink is resolved by frontmatter title
- **THEN** it resolves regardless of which style named the target file
