## ADDED Requirements

### Requirement: Vault-Level Filename Style

Exomem SHALL resolve a single filename style per vault rather than per call. The
style SHALL be one of `slug` (lowercase ASCII kebab-case, today's behaviour) or
`title` (the human title preserved, sanitised only for filesystem safety). For a
compiled note the default SHALL remain `slug`, so an existing install's derived
names do not change on upgrade. An entity follows the requirement "Entity
Filenames Follow The Vault Naming Path".

Resolution order SHALL be: an explicit per-call `slug` argument, then
`EXOMEM_FILENAME_STYLE`, then the vault's `_Schema/project-keys.yaml` key, then
the built-in default. An explicit per-call `slug` SHALL keep its current strict
contract — lowercase ASCII kebab-case — under every style, because it is how a
caller pins a name it intends to link to.

#### Scenario: A vault with no configuration behaves as it does today

- **WHEN** a note is written to a vault that sets no filename style anywhere
- **THEN** the derived filename is the lowercase kebab slug of the title
- **AND** it is byte-identical to the name the same call produced before this change

#### Scenario: Title style preserves the title on disk

- **GIVEN** a vault configured with filename style `title`
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
host's own. Exomem SHALL remove the characters `< > : " / \ | ? *`, all control
characters, and any trailing dot or space; SHALL rename a title that resolves to
a Windows reserved device name (`CON`, `PRN`, `AUX`, `NUL`, `COM1`-`COM9`,
`LPT1`-`LPT9`); and SHALL normalise the result to NFC. A title that sanitises to
an empty string SHALL fall back to the slug style for that note rather than
producing an unnamed file.

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
truncation warning, because a truncated name cannot be re-derived from the title
and is therefore the name a caller must link to.

#### Scenario: The default cap is unchanged

- **WHEN** a vault sets no length configuration
- **THEN** derived names are capped at 100 characters as before

#### Scenario: A shorter cap still warns that the name is not re-derivable

- **GIVEN** a vault configured with a derived-name cap of 60
- **WHEN** a note whose title would derive a longer name is written
- **THEN** the name is truncated on a word boundary at or below 60 characters
- **AND** the response carries the truncation warning naming the resulting name

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
Names". The entity's title SHALL be stored as given. When neither the environment
nor the vault configures a style, an entity SHALL be named in `title` style. A
vault that configures `slug` SHALL name its entities in `slug` style, and an
explicit `slug` argument SHALL name the file under every style.

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
