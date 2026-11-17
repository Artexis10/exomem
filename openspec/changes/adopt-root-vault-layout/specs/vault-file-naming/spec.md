## MODIFIED Requirements

### Requirement: Case-Insensitive Collision Detection

Because `title` style preserves capitals, two distinct titles can differ only by
case and collide on a case-insensitive filesystem. Exomem SHALL detect a
case-insensitive collision with an existing file before writing, and SHALL
disambiguate through the same mechanism the `slug` style already uses, so a write
never silently overwrites an unrelated note. In a `root` vault, a name derived
under `title` style SHALL follow the `vault-layout` requirement "Derived names are
unique across the vault" instead: uniqueness across the whole vault and a ` (2)`
suffix. A slug name, and a `title` name in a `nested` vault, SHALL keep the
mechanism of this requirement.

#### Scenario: Titles differing only by case do not overwrite each other

- **GIVEN** a vault configured with filename style `title` containing `Budget Review.md`
- **WHEN** a note titled `budget review` is written
- **THEN** the existing file's content is unchanged
- **AND** the new note is written under a disambiguated name

#### Scenario: A root vault numbers a colliding title name with a suffix in brackets

- **GIVEN** a `root` vault whose `Insights/` folder holds `Garden Plan.md`
- **WHEN** a note titled `garden plan` is written to another area
- **THEN** the existing file's content is unchanged
- **AND** the new note is named `garden plan (2).md`

### Requirement: No Existing File Is Renamed

This capability SHALL govern the derivation of new filenames only, in both
layouts. Changing a vault's filename style SHALL NOT rename, move, or rewrite any
file already on disk, and SHALL NOT alter any wikilink already written, so no link
that resolves before the change stops resolving after it. A retitle SHALL NOT
rename a file: after a governed write or an outside edit changes a page's title,
the page SHALL keep its name and no link SHALL be repointed. This requirement is
the single statement of the retitle rule; the `vault-layout` requirement "A page
name is derived or fixed" refers to it. The single exception is the relayout of
the `vault-layout` capability, which gives each existing derived name the form of
the vault's resolved filename style when it moves a vault to the `root` layout.

#### Scenario: Switching style leaves the vault untouched

- **GIVEN** a vault of existing notes written under `slug` style
- **WHEN** the vault is reconfigured to `title` style
- **THEN** no existing file is renamed and no existing link target is rewritten
- **AND** subsequently written notes use the new style

#### Scenario: A mixed vault resolves links under both styles

- **GIVEN** a vault containing notes named under both styles
- **WHEN** a wikilink is resolved by frontmatter title
- **THEN** it resolves regardless of which style named the target file

#### Scenario: A retitle keeps the file name

- **WHEN** a governed write changes the title of a page with a derived name, in a `root` vault or a `nested` vault
- **THEN** the page keeps its path
- **AND** no link is repointed

#### Scenario: An outside edit to a title keeps the file name

- **WHEN** a person edits a page's title in an editor and the outside-edit import records the edit as a governed write
- **THEN** the page keeps its path

#### Scenario: A style change renames nothing in either layout

- **WHEN** the filename style of a vault of either layout changes and a governed write then edits a page whose name was derived under the earlier style
- **THEN** the page keeps its path
- **AND** no link is repointed

#### Scenario: The relayout renames a derived name to its title name

- **GIVEN** a `nested` vault whose compiled note titled `Churn rose in Q3` is named `churn-rose-in-q3.md`
- **WHEN** the relayout moves the vault to the `root` layout
- **THEN** the note is named `Churn rose in Q3.md` in its area
- **AND** every link that resolved to the note before the relayout resolves to it after
