# vault-file-naming Specification

## Purpose
A new page is named by its title unless the vault opts out with the `slug` style. Names stay safe for every filesystem and every wikilink, stay unique across case and Unicode form, and never change for an existing file. A page whose name carries a date keeps its slug name unless the vault chose a style. The doctor reports the style, and public replies lead with the title.

## Requirements

### Requirement: Vault-Level Filename Style

Exomem SHALL resolve a single filename style per vault rather than per call. The
style SHALL be one of `slug` (lowercase ASCII kebab-case, today's behaviour) or
`title` (the human title preserved, corrected for filesystem and link safety).
The default SHALL be `title` for a new compiled note and a new entity, and `slug`
SHALL remain available as the opt-out. A Source, whose name follows its capture
date, and an experiment or production log, whose name carries a month, SHALL keep
their dated slug names in a vault that sets no style. An entity
follows the requirement "Entity Filenames Follow The Vault Naming Path".

Resolution order SHALL be: an explicit per-call `slug` argument, then
`EXOMEM_FILENAME_STYLE`, then the vault's `_Schema/project-keys.yaml` key, then
the built-in default. An explicit per-call `slug` SHALL keep its current strict
contract — lowercase ASCII kebab-case — under every style, because it is how a
caller pins a name it intends to link to.

#### Scenario: A vault with no configuration names a new note by its title

- **WHEN** a note titled `Zöe Exämple` is written to a vault that sets no filename style anywhere
- **THEN** the file is named `Zöe Exämple.md`
- **AND** the stored title is `Zöe Exämple`

#### Scenario: A vault that chooses slug keeps today's names

- **GIVEN** a vault whose `project-keys.yaml` sets filename style `slug`
- **WHEN** a note is written with no explicit `slug`
- **THEN** the derived filename is the lowercase kebab slug of the title
- **AND** it is byte-identical to the name the same call produced before the default changed

#### Scenario: A Source keeps its dated slug name

- **WHEN** a Source is captured in a vault that sets no filename style
- **THEN** the file is named `<date>-<kebab slug of the title>.md`

#### Scenario: An experiment keeps its dated slug name

- **WHEN** an experiment started in May 2026 is written to a vault that sets no filename style
- **THEN** the file is named `2026-05-<kebab slug of the title>.md`
- **AND** a vault that configures `title` names it `2026-05-<title>.md`

#### Scenario: Title style preserves the title on disk

- **GIVEN** a vault configured with filename style `title`, or one that sets none
- **WHEN** a note titled `Exomem first-run defect inventory — issues 477 to 485` is written
- **THEN** the file is named `Exomem first-run defect inventory — issues 477 to 485.md`
- **AND** capitals, spaces, and the em dash are preserved

#### Scenario: An explicit slug still wins under title style

- **GIVEN** a vault configured with filename style `title`
- **WHEN** a caller passes `slug="quarterly-review"` alongside a different title
- **THEN** the file is named `quarterly-review.md`
- **AND** a non-kebab explicit slug is still rejected as invalid

#### Scenario: Environment overrides the vault key

- **GIVEN** a vault whose `project-keys.yaml` sets filename style `title`
- **WHEN** `EXOMEM_FILENAME_STYLE=slug` is set in the environment
- **THEN** the derived filename is the kebab slug

### Requirement: Filesystem-Safe Title Names

Under `title` style, a derived filename SHALL be safe on Windows, macOS and Linux
alike, by applying the union of their restrictions everywhere rather than the
host's own, and SHALL be safe as a wikilink target. Exomem SHALL remove the
characters `< > : " / \ | ? *`, all control characters, and any trailing dot or
space; SHALL replace each of `# [ ] ^ |` with a space and drop leading dots, so
that a link to the name resolves; SHALL rename a title that resolves to a Windows
reserved device name (`CON`, `PRN`, `AUX`, `NUL`, `COM1`-`COM9`, `LPT1`-`LPT9`);
SHALL cut the name under the filename byte cap; and SHALL normalise the result to
NFC. A title that sanitises to an empty string SHALL fall back to the slug style
for that note rather than producing an unnamed file.

#### Scenario: Reserved characters are removed, not transliterated

- **GIVEN** a vault configured with filename style `title`
- **WHEN** a note titled `Q3: revenue / margin — what "held"?` is written
- **THEN** the filename contains no `:`, `/`, `"` or `?` character
- **AND** the words, spaces and em dash that surrounded them are preserved

#### Scenario: A reserved device name does not become a file

- **GIVEN** a vault configured with filename style `title`
- **WHEN** a note titled `NUL` is written
- **THEN** the filename is not `NUL.md`
- **AND** the note is created successfully

#### Scenario: A title that sanitises away still produces a file

- **GIVEN** a vault configured with filename style `title`
- **WHEN** a note whose title consists only of reserved characters is written
- **THEN** the filename falls back to the slug style
- **AND** the write succeeds rather than failing

#### Scenario: A name authored on one platform opens on another

- **WHEN** any note is written under `title` style on any supported platform
- **THEN** the resulting filename is representable on Windows, macOS and Linux

### Requirement: A Changed Filename Is Reported

Exomem SHALL store the title exactly as given. When the derived filename differs
from the title after the corrections above, the write SHALL carry one warning
that names each correction and says whether the name was cut. The warning SHALL
name the file by its name and the page by its title, never by a storage path.

#### Scenario: A link character is replaced and the write says so

- **GIVEN** a vault that sets no filename style
- **WHEN** a note titled `Plan #3 [draft]` is written
- **THEN** the file is named `Plan 3 draft.md`
- **AND** the stored title is `Plan #3 [draft]`
- **AND** exactly one `FILENAME_ADJUSTED` warning names the replaced link characters
- **AND** a wikilink to `Plan 3 draft` resolves to the page

#### Scenario: A very long title is cut under the byte cap

- **WHEN** a note whose title is longer than the byte cap allows is written
- **THEN** the filename is at most 200 bytes
- **AND** the warning says the name was shortened

#### Scenario: A clean name carries no warning

- **WHEN** a note titled `Zöe Exämple` is written
- **THEN** the write carries no `FILENAME_ADJUSTED` warning

### Requirement: Doctor Reports The Filename Style

`exomem doctor` SHALL report the filename style in force and its source: the
environment, the vault's `project-keys.yaml`, or the default. An unrecognised
configured value SHALL appear as a failed check, not as an error from the
command.

#### Scenario: The report names the style and where it came from

- **GIVEN** a vault that sets no filename style
- **WHEN** `exomem doctor` runs
- **THEN** the report says new pages are named in the `title` style, from the default

### Requirement: Case-Insensitive Collision Detection

Because `title` style preserves capitals, two distinct titles can differ only by
case and collide on a case-insensitive filesystem. Exomem SHALL detect a
case-insensitive collision with an existing file before writing, and SHALL
disambiguate through the same mechanism the `slug` style already uses, so a write
never silently overwrites an unrelated note.

#### Scenario: Titles differing only by case do not overwrite each other

- **GIVEN** a vault configured with filename style `title` containing `Budget Review.md`
- **WHEN** a note titled `budget review` is written
- **THEN** the existing file's content is unchanged
- **AND** the new note is written under a disambiguated name

### Requirement: Configurable Derived-Name Length

The cap on a derived filename SHALL be configurable per vault, defaulting to the
current 100 characters so no existing vault's names change. Truncation SHALL
continue to fall on a word boundary and SHALL continue to emit the existing
warning, because a truncated name cannot be re-derived from the title and is
therefore the name a caller must link to. Under `title` style that warning is the
`FILENAME_ADJUSTED` warning above.

#### Scenario: The default cap is unchanged

- **WHEN** a vault sets no length configuration
- **THEN** derived names are capped at 100 characters as before

#### Scenario: A shorter cap still warns that the name is not re-derivable

- **GIVEN** a vault configured with a derived-name cap of 60
- **WHEN** a note whose title would derive a longer name is written
- **THEN** the name is truncated on a word boundary at or below 60 characters
- **AND** the response carries a warning naming the resulting name

### Requirement: No Existing File Is Renamed

This capability SHALL govern the derivation of new filenames only. Changing a
vault's filename style SHALL NOT rename, move, or rewrite any file already on
disk, and SHALL NOT alter any wikilink already written, so no link that resolves
before the change stops resolving after it.

#### Scenario: Switching style leaves the vault untouched

- **GIVEN** a vault of existing notes written under `slug` style
- **WHEN** the vault is reconfigured to `title` style
- **THEN** no existing file is renamed and no existing link target is rewritten
- **AND** subsequently written notes use the new style

#### Scenario: A mixed vault resolves links under both styles

- **GIVEN** a vault containing notes named under both styles
- **WHEN** a wikilink is resolved by frontmatter title
- **THEN** it resolves regardless of which style named the target file

### Requirement: Entity Filenames Follow The Vault Naming Path

Exomem SHALL derive an entity page's filename through the same vault naming path
as a compiled note. The filename SHALL keep the name as written, including
diacritics, case and punctuation, after the removals under "Filesystem-Safe Title
Names". The entity's title SHALL be stored as given. An entity SHALL follow the vault's
filename style, which is `title` unless the environment or the vault configures
`slug`. An explicit `slug` argument SHALL name the file under every style.

#### Scenario: A name keeps its diacritics and case on disk

- **WHEN** an entity named `Zöe Exämple` is created in a vault that sets no style
- **THEN** the file is named `Zöe Exämple.md`
- **AND** the entity's title is `Zöe Exämple`

#### Scenario: Reserved characters are removed from an entity filename

- **WHEN** an entity named `Draft: A/B plan? <v2>` is created
- **THEN** the filename contains none of `:`, `/`, `?`, `<` or `>`
- **AND** the entity's title is stored as given

#### Scenario: An NFD name is written as NFC

- **WHEN** an entity name arrives in decomposed Unicode form
- **THEN** the filename is the NFC form of that name

### Requirement: Entity Creation Refuses A Filename Clash

Entity creation SHALL refuse a name when the destination folder already holds a
leaf that equals the derived name after case folding and NFC normalisation. The
refusal SHALL be `ENTITY_EXISTS`, SHALL name the page that holds the name, and
SHALL leave that page unchanged. An agent on the public tool shape SHALL see that
page named by its title, never by its storage path.

#### Scenario: Two titles that map to one file do not overwrite each other

- **GIVEN** an entity named `Orbit Lab` exists
- **WHEN** an entity named `orbit lab?` is created in the same folder
- **THEN** the call is refused with `ENTITY_EXISTS`
- **AND** the existing page's bytes are unchanged

### Requirement: A Public Page Read And An Entity Candidate Lead With The Title

On the public tool shape, a page read and each resolve-entity candidate SHALL
lead with the page title and carry the handles that every tool accepts (`id`, and
`ref` where the page has one). The storage path SHALL move under `diagnostics`.
A page whose bytes cannot be read, so that it has no handle, SHALL keep its path.

#### Scenario: A page read leads with the title and its id opens the page

- **WHEN** an agent reads a page through `read_page`
- **THEN** the reply starts with the page title and carries `id`
- **AND** the path is under `diagnostics`, not at the top level
- **AND** passing that `id` or `ref` to `read_page` returns the same page

#### Scenario: A resolve-entity candidate leads with the title

- **WHEN** an agent resolves an entity name through `connect_memory`
- **THEN** each candidate starts with the entity title and carries `id` and `ref`
- **AND** the candidate's path is under its `diagnostics`
