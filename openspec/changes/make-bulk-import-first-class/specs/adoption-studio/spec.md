## ADDED Requirements

### Requirement: Own notes adopt as governed draft notes

A `select` folder rule SHALL accept `adopt_as` with the values `source` (the default) and `note`. A `note` rule SHALL name a `note_type` that the note-type registry holds as a compiled type, and MAY name the `project` or `domain` that the type's destination needs. `select` SHALL refuse an unregistered note type, or a type the note leaf cannot place, with a coded error and no selection change. The rule SHALL be part of the selection identity. The server SHALL NOT infer whether a file is an own note from its words or name.

For an own-note item, `plan` SHALL preview the Source copy exactly as for a `source` item and SHALL also preview a note target, title and front matter. The note body SHALL be the original note body after its front matter; its title, tags and aliases SHALL come from the parsed front matter. The note's `sources:` SHALL cite the item's Source copy, and the note SHALL carry the item's `import_collection` and `collection_path`. A link from an own note to another own-note item SHALL resolve to that item's planned note; any other resolved link SHALL resolve as it does for Sources. The note target SHALL be part of the planned actions hashed into `plan_id`.

`apply` SHALL commit each chunk's Sources as for `source` items, then create each own-note item's note with status `draft` through the governed two-phase note creation, recording the note's outcome in the run object with its commit. An own-note item SHALL be complete only when both its Source and its note exist. The identity check SHALL find a note that an earlier run, a retry or a crashed apply created for the same collection path and record it as `already-applied` instead of creating another. An occupied note target SHALL fail the item with `TARGET_OCCUPIED`. For an item whose original changed since an earlier import, `plan` SHALL record the earlier note as `previous_note`, and `apply` SHALL create only the new Source.

Own-note creation SHALL only create pages that did not exist; it SHALL NOT edit, move or delete an existing page. The plan confirmation that `apply` binds to `plan_id` SHALL be the only confirmation the import needs.

#### Scenario: A folder of own notes becomes draft notes with lossless Sources

- **WHEN** a collection's `Journal/` folder is selected with `adopt_as: note` and a registered `note_type`, and its `Exports/` folder is selected with no rule, and `apply` runs to completion
- **THEN** each journal file has a Source whose fenced capture hashes to its `original_sha256` and a draft note whose body is the original body and whose `sources:` cites that Source
- **AND** each export file has a Source and no note
- **AND** every original is byte-identical

#### Scenario: Links between own notes point at notes

- **WHEN** an own note links to a second own note and embeds an image from the collection
- **THEN** the first note's link resolves to the second item's note and the embed resolves to the image's Source

#### Scenario: A re-run creates no second note

- **WHEN** the same collection is staged again unchanged and planned and applied
- **THEN** every own-note item records `already-applied` with its earlier Source and note, and no page is created

#### Scenario: An unregistered note type is refused

- **WHEN** `select` receives a `note` rule whose `note_type` the registry does not hold
- **THEN** it refuses with a coded error and the selection is unchanged

#### Scenario: A budget stop between notes resumes

- **WHEN** `apply` reaches its request budget after committing a chunk's Sources and some of its notes
- **THEN** it returns a continuation, and the next call creates only the remaining notes

### Requirement: A folder outside the vault imports through local staging

`adoption_studio start` SHALL refuse a `path` that is absolute or that resolves outside the vault with `IMPORT_PATH_OUTSIDE_VAULT`, and the message SHALL name `exomem import <folder>`.

The `exomem import <folder>` command SHALL enumerate the folder without following symbolic links and SHALL report the file count and bytes against the effective limits before copying: the free space of the vault's filesystem and the limits the adoption engine enforces. It SHALL refuse before copying, with the measured value and the limit, when a limit would be exceeded. Otherwise it SHALL copy each regular file into `_Imports/<import_id>/<folder name>/`, streaming, record a manifest of relative path, size and SHA-256, re-hash every copy, report progress while copying, and end with the `adoption_studio start` path. A symbolic link, special file or unreadable file SHALL get a per-path code in the manifest. The command SHALL never modify the original folder. An interrupted copy SHALL resume from its manifest under the same import id without copying a verified file again.

#### Scenario: A local folder is staged and adopted

- **WHEN** `exomem import ~/Notes` runs for a folder outside the vault and adoption then starts on the printed path
- **THEN** the staged files' hashes equal the originals', the manifest accounts for every entry, and the collection label is the folder's name

#### Scenario: A folder too large for the disk is refused before copying

- **WHEN** the folder's bytes exceed the vault filesystem's free space
- **THEN** the command refuses with the measured bytes and the free space, and copies nothing

#### Scenario: An outside path given to start is refused with the route

- **WHEN** `adoption_studio start` receives the absolute path of a folder outside the vault
- **THEN** it refuses with `IMPORT_PATH_OUTSIDE_VAULT` and names `exomem import`

## MODIFIED Requirements

### Requirement: Cloud staged imports adopt through the local engine

On Exomem Cloud, an `adopt` import SHALL land as RAW files under the vault-relative staging directory `_Imports/<import_id>/`, outside `Knowledge Base/` so the engine treats them as legacy input. The Cloud import job SHALL unpack and validate the archive before the folder appears there, with its member, path and size checks. `adoption_studio(action="start")` SHALL accept `path` pointing at that staging directory and scan it with the identical engine used locally. A local run SHALL scan a folder inside the vault in place, and SHALL stage only a folder outside the vault, through `exomem import`, into the same layout.

#### Scenario: A staged Cloud import adopts through the local engine

- **WHEN** an `adopt` import has finished and its files are in `_Imports/<import_id>/`
- **THEN** `adoption_studio(start, path="_Imports/<import_id>")` scans the staged files through the same engine as a local run

#### Scenario: A folder inside the vault is not staged

- **WHEN** a local run starts on a folder inside the vault and outside `Knowledge Base/`
- **THEN** the engine scans it in place and creates no `_Imports/` copy
