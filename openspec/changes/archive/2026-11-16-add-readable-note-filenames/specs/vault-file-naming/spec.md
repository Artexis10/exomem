## ADDED Requirements

### Requirement: Vault-Level Filename Style

Exomem SHALL resolve a single filename style per vault rather than per call. The
style SHALL be one of `slug` (lowercase ASCII kebab-case, today's behaviour) or
`title` (the human title preserved, corrected for filesystem and link safety).
The default SHALL be `title` for a new compiled note and a new entity, and `slug`
SHALL remain available as the opt-out. A Source, whose name follows its capture
date, SHALL keep its dated slug name in a vault that sets no style. A note whose
name starts with a month, such as an experiment or a production log, SHALL follow
the default and keep its date prefix. An entity
follows the requirement "Entity Filenames Follow The Vault Naming Path".

Resolution order SHALL be: an explicit per-call `slug` argument, then
`EXOMEM_FILENAME_STYLE`, then the vault's `_Schema/project-keys.yaml` key, then
the built-in default. An explicit per-call `slug` SHALL keep its current strict
contract — lowercase ASCII kebab-case — under every style, because it is how a
caller pins a name it intends to link to.

A configured style that Exomem cannot use SHALL refuse the write with
`INVALID_SLUG`, naming the source, and SHALL NOT fall back to the default,
because the default would silently name pages in a style that the vault did not
choose. A style value that is not one of the known styles, is not a string, or is
empty, and a `project-keys.yaml` that cannot be read or is not a mapping of keys,
all refuse. Only a missing file, or a file with no style key, means "no style
set". The environment value, when set, applies without reading the file.

#### Scenario: A vault with no configuration names a new note by its title

- **WHEN** a note titled `Brûlerie Ötzberg` is written to a vault that sets no filename style anywhere
- **THEN** the file is named `Brûlerie Ötzberg.md`
- **AND** the stored title is `Brûlerie Ötzberg`

#### Scenario: A vault that chooses slug keeps today's names

- **GIVEN** a vault whose `project-keys.yaml` sets filename style `slug`
- **WHEN** a note is written with no explicit `slug`
- **THEN** the derived filename is the lowercase kebab slug of the title
- **AND** it is byte-identical to the name the same call produced before the default changed

#### Scenario: A Source keeps its dated slug name

- **WHEN** a Source is captured in a vault that sets no filename style
- **THEN** the file is named `<date>-<kebab slug of the title>.md`

#### Scenario: An experiment is named by its title after its date prefix

- **WHEN** an experiment started in May 2026 and titled `Brûlée Soak Trial` is written to a vault that sets no filename style
- **THEN** the file is named `2026-05-Brûlée Soak Trial.md`
- **AND** the stored title is `Brûlée Soak Trial`

#### Scenario: An experiment in a slug vault keeps its dated slug name

- **GIVEN** a vault whose `project-keys.yaml` sets filename style `slug`
- **WHEN** an experiment started in May 2026 and titled `Brûlée Soak Trial` is written
- **THEN** the file is named `2026-05-brulee-soak-trial.md`

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

#### Scenario: A style key that cannot be read refuses instead of taking the default

- **GIVEN** a vault whose `project-keys.yaml` sets `filename_style` to a list, a number or an empty value, or cannot be parsed
- **WHEN** a note is written with no explicit `slug`
- **THEN** the write is refused with `INVALID_SLUG`
- **AND** the reason names `project-keys.yaml` and the value or the read failure
- **AND** a vault with no `project-keys.yaml`, or one without the key, still takes the default

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
that a link to the name resolves; SHALL treat a title that resolves to a Windows
reserved device name (`CON`, `PRN`, `AUX`, `NUL`, `COM1`-`COM9`, `LPT1`-`LPT9`) as
having no usable name; SHALL cut the name under the filename byte cap; and SHALL
normalise the result to NFC. A title that sanitises to an empty string, or that is
a reserved device name, SHALL fall back to the slug style for that note rather than
producing an unnamed file. The slug of a device name is the same word in lowercase
(`nul.md`), which Windows still reserves: this capability does not rename it.

#### Scenario: Reserved characters are removed, not transliterated

- **GIVEN** a vault configured with filename style `title`
- **WHEN** a note titled `Q3: revenue / margin — what "held"?` is written
- **THEN** the filename contains no `:`, `/`, `"` or `?` character
- **AND** the words, spaces and em dash that surrounded them are preserved

#### Scenario: A reserved device name falls back to the slug style

- **GIVEN** a vault configured with filename style `title`
- **WHEN** a note titled `NUL` is written
- **THEN** the filename is the slug `nul.md`, not `NUL.md`
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

Exomem SHALL store the title exactly as given. When the file name that a write
finally uses differs from the title (in NFC), the write SHALL carry one
`FILENAME_ADJUSTED` warning. The warning SHALL be built where the final name is
known, after any collision number is added, and SHALL name that name: it is the
stem that a link resolves by. It SHALL say whether characters were changed, the
name was cut, or a number was added. It SHALL name the file by its name, never by
a storage path.

#### Scenario: A link character is replaced and the write says so

- **GIVEN** a vault that sets no filename style
- **WHEN** a note titled `Plan #3 [draft]` is written
- **THEN** the file is named `Plan 3 draft.md`
- **AND** the stored title is `Plan #3 [draft]`
- **AND** exactly one `FILENAME_ADJUSTED` warning names `Plan 3 draft` and says that characters were changed
- **AND** a wikilink to `Plan 3 draft` resolves to the page

#### Scenario: A character that the file-name rules remove is reported

- **WHEN** a note titled `Plan | B` is written
- **THEN** the file is named `Plan B.md`
- **AND** one `FILENAME_ADJUSTED` warning names `Plan B`

#### Scenario: A second page with the same name is reported by its own name

- **GIVEN** a note titled `Plan #3 [draft]` is stored as `Plan 3 draft.md`
- **WHEN** a second note with that title is written
- **THEN** the file is named `Plan 3 draft-2.md`
- **AND** its `FILENAME_ADJUSTED` warning names `Plan 3 draft-2` and says that a number was added
- **AND** a wikilink to `Plan 3 draft-2` resolves to the second page

#### Scenario: A very long title is cut under the byte cap

- **WHEN** a note whose title is longer than the byte cap allows is written
- **THEN** the filename is at most 200 bytes
- **AND** the warning says the name was shortened

#### Scenario: A collision number counts toward the byte cap

- **GIVEN** a note whose title makes a file name of 197 bytes exists
- **WHEN** a second note with the same title is written
- **THEN** the title part is cut at a character so that the name, the number and `.md` are at most 200 bytes
- **AND** the warning says that the name was shortened and a number was added

#### Scenario: A date prefix counts toward the byte cap

- **WHEN** an experiment is written whose title fits the byte cap alone but not beside its `YYYY-MM-` prefix
- **THEN** the title part is cut so that the whole filename is at most 200 bytes
- **AND** the warning says the name was shortened

#### Scenario: A clean name carries no warning

- **WHEN** a note titled `Brûlerie Ötzberg` is written
- **THEN** the write carries no `FILENAME_ADJUSTED` warning

### Requirement: Doctor Reports The Filename Style

`exomem doctor` SHALL report the filename style in force and its source: the
environment, the vault's `project-keys.yaml`, or the default. A configured value
that Exomem cannot use SHALL appear as a failed check that names the source, not
as an error from the command.

#### Scenario: The report names the style and where it came from

- **GIVEN** a vault that sets no filename style
- **WHEN** `exomem doctor` runs
- **THEN** the report says new pages are named in the `title` style, from the default

#### Scenario: A malformed style key is a failed check

- **GIVEN** a vault whose `project-keys.yaml` sets `filename_style` to a list
- **WHEN** `exomem doctor` runs
- **THEN** the `vault.filename_style` check fails and says that the value in `project-keys.yaml` is not one of the styles

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

### Requirement: A New Name Does Not Take A Live Page's Title

A stem match beats a title match when a bare wikilink resolves. In `title` style,
a page whose file is named by its old slug still holds its title, so a new page
named by the same title would take over every bare link to that title. When a new
page's name is chosen, Exomem SHALL count a live page's title (in NFC, case
folded) as taking that stem, and the new page SHALL take the collision number, so
that a bare link finds no stem match and reaches the title step, which reports the
duplicate as ambiguous. The lookup SHALL NOT scan the vault: it SHALL use the
title index that a running process already holds, and a process that holds none
names the page from its folder alone. An explicit per-call `slug`, and a note
whose name an adoption plan froze, SHALL NOT be moved by a live title.

#### Scenario: A new page does not take over a link to an older page with its title

- **GIVEN** a page titled `Brûlerie Ötzberg` stored under its old slug `brulerie-otzberg.md`, and a running process that holds the title index
- **WHEN** a second note titled `Brûlerie Ötzberg` is written in `title` style
- **THEN** the second file is named `Brûlerie Ötzberg-2.md`
- **AND** its `FILENAME_ADJUSTED` warning names `Brûlerie Ötzberg-2` and says that a number was added
- **AND** a bare link to `Brûlerie Ötzberg` is reported as matching two files by title, and is not moved to the second page

#### Scenario: A process without a title index names the page from its folder

- **GIVEN** a page titled `Brûlerie Ötzberg` stored under its old slug, and a process that holds no title index
- **WHEN** a second note with that title is written
- **THEN** the write builds no title index and scans no vault
- **AND** the file is named `Brûlerie Ötzberg.md`

### Requirement: Derived-Name Length Is Capped

Two fixed caps SHALL bound a derived file name, and no vault setting SHALL change
either. A name derived from a title SHALL be cut to 100 characters, on a word
boundary. The whole file name, with its date prefix, collision number and
extension, SHALL be at most 200 bytes; the title part is the only part that
shrinks, and it is cut at a character. A cut name cannot be re-derived from the
title, so it is the name a caller must link to. Under `title` style the
`FILENAME_ADJUSTED` warning above reports it.

#### Scenario: A long title is cut on a word boundary

- **WHEN** a note whose title is longer than 100 characters is written
- **THEN** the derived name is at most 100 characters and ends on a whole word
- **AND** the response carries a warning naming the resulting name

#### Scenario: No vault key moves the cap

- **WHEN** a vault's `project-keys.yaml` carries any length setting
- **THEN** derived names are still capped at 100 characters and 200 bytes

### Requirement: An Adoption Plan Keeps The Vault Style On A Taken Name

A note that an adoption plan names, where its target is already taken or already
planned, SHALL be named through the same path as a live write: the vault's style
and then the collision number. The plan SHALL freeze that number with the target,
and applying the plan SHALL land the note at the frozen target.

#### Scenario: Three files with one title are planned as a numbered family

- **GIVEN** a vault that sets no filename style
- **WHEN** an adoption plan covers three files whose title is `Entry`
- **THEN** the planned notes are `Entry.md`, `Entry-2.md` and `Entry-3.md`
- **AND** applying the plan creates those three files
- **AND** each numbered note carries the `FILENAME_ADJUSTED` warning that a live write would carry

#### Scenario: A slug vault plans the slug family

- **GIVEN** a vault configured with filename style `slug`
- **WHEN** an adoption plan covers three files whose title is `Entry`
- **THEN** the planned notes are `entry.md`, `entry-2.md` and `entry-3.md`

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

- **WHEN** an entity named `Brûlerie Ötzberg` is created in a vault that sets no style
- **THEN** the file is named `Brûlerie Ötzberg.md`
- **AND** the entity's title is `Brûlerie Ötzberg`

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
SHALL leave that page unchanged. A refusal because another entity holds the title
or an alias SHALL name that entity too. An agent on the public tool shape SHALL see
each such page named by its title and `id`, never by its storage path.

#### Scenario: Two titles that map to one file do not overwrite each other

- **GIVEN** an entity named `Orbit Lab` exists
- **WHEN** an entity named `orbit lab?` is created in the same folder
- **THEN** the call is refused with `ENTITY_EXISTS`
- **AND** the existing page's bytes are unchanged

#### Scenario: A refused alias names the page that holds it

- **GIVEN** an entity answers to the alias `Vexa`
- **WHEN** another entity is created with that alias through the public tool shape
- **THEN** the call is refused with `ENTITY_EXISTS`
- **AND** the refusal names the holder by its title and an `id` that opens it, with no storage path

### Requirement: A Public Page Read And An Entity Candidate Lead With The Title

On the public tool shape, a page read and each resolve-entity candidate SHALL
lead with the page title and carry the handles that every tool accepts (`id`, and
`ref` where the page has one). The storage path SHALL stay at the top level, where
clients read it. A page whose bytes cannot be read, so that it has no handle,
SHALL be returned as it was.

#### Scenario: A page read leads with the title and its id opens the page

- **WHEN** an agent reads a page through `read_page`
- **THEN** the reply starts with the page title and carries `id`
- **AND** the path stays at the top level
- **AND** passing that `id` or `ref` to `read_page` returns the same page

#### Scenario: A resolve-entity candidate leads with the title

- **WHEN** an agent resolves an entity name through `connect_memory`
- **THEN** each candidate starts with the entity title and carries `id` and `ref`
- **AND** the candidate keeps its `path`
