## ADDED Requirements

### Requirement: Own notes adopt as governed draft notes

A `select` folder rule SHALL accept `adopt_as` with the values `source` (the default) and `note`. A `note` rule SHALL name a `note_type` that the note-type registry holds as a compiled type, and MAY name the `project` or `domain` that the type's destination needs. `select` SHALL refuse an unregistered note type, or a type the note leaf cannot place, with a coded error and no selection change. The rule SHALL be part of the selection identity. The server SHALL NOT infer whether a file is an own note from its words or name.

For an own-note item, `plan` SHALL preview the Source copy exactly as for a `source` item and SHALL also preview a note target, title and front matter. The note body SHALL be the original note body after its front matter, unchanged, followed by the generated `## Links` section that a Source carries; its title, tags and aliases SHALL come from the parsed front matter, and its title is the front matter's `title` when it has one. The note's `sources:` SHALL cite the item's Source copy, and the note SHALL carry the item's `import_collection` and `collection_path`. A link from an own note to another own-note item SHALL resolve to that item's planned note; any other resolved link SHALL resolve as it does for Sources. The note target SHALL be part of the planned actions hashed into `plan_id`.

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

`adoption_studio start` SHALL refuse a `path` that is absolute, or that climbs out of the vault by `..`, with `IMPORT_PATH_OUTSIDE_VAULT`, and the message SHALL name `exomem import <folder>`. The check SHALL read the path as text and SHALL NOT resolve it on the filesystem. A path that escapes through a symbolic link inside the vault SHALL get the overview resolver's `INVALID_PATH`.

The `exomem import <folder>` command SHALL enumerate the folder without following symbolic links, and SHALL select files by these rules:

- A hidden entry (a name that starts with a dot, or the Windows hidden attribute), at any depth, SHALL be left behind unless the user passes `--include-hidden`.
- The `.gitignore` files inside the folder SHALL apply, with git's precedence, unless the user passes `--no-gitignore`. The command SHALL NOT hard-code a folder name to leave behind.
- A symbolic link or a Windows junction at any depth SHALL be left behind and never followed. A special file and an unreadable file SHALL be left behind with their own reasons.

Before it copies, the command SHALL report what it leaves behind, with the count and bytes under each reason, and SHALL report the selected file count and bytes against the free space of the vault's filesystem. It SHALL refuse before copying, with the measured bytes and the free space, when the copy plus a reserve does not fit. The limits the adoption engine applies after staging SHALL be warnings, not refusals. Otherwise it SHALL copy each selected regular file into `_Imports/<import_id>/<folder name>/`, streaming, record a manifest of relative path, size and SHA-256, re-hash every copy, and report progress while copying. Every entry left behind SHALL get a per-path code in the manifest. The command SHALL never modify the original folder, and the folder's name SHALL be the collection label. A file that changed while it was read, could not be read, or was refused by the disk SHALL be left out with a code and SHALL keep the import open: the command SHALL exit with an error and say that the same command run again retries only those files under the same import id. A file that the vault refuses because of its own name or path SHALL be left out with the vault's code and SHALL NOT keep the import open: the import SHALL finish, and the report SHALL give the count of such files. An interrupted or incomplete copy SHALL resume from its manifest under the same import id without copying a verified file again. On a resume, a staged file that the folder no longer selects SHALL be removed and recorded in the manifest, and a staged file that changed after it was verified SHALL be copied again. The command SHALL name an unfinished import made with other options and SHALL NOT resume it, and SHALL NOT let two runs write the same import at once.

When the import finishes, the command SHALL end by reporting the bytes it staged, stating that the staged folder is a second copy of the data on the disk until it is discarded, naming the discard route that exists, and printing the `adoption_studio start` path. An import that stays open SHALL also print that path, as the way to adopt what is staged so far.

#### Scenario: A local folder is staged and adopted

- **WHEN** `exomem import ~/Notes` runs for a folder outside the vault and adoption then starts on the printed path
- **THEN** the staged files' hashes equal the originals', the manifest accounts for every entry, and the collection label is the folder's name

#### Scenario: Hidden entries, ignored files and links stay behind and are reported

- **WHEN** the folder holds a hidden folder, a file that its `.gitignore` names, and a symbolic link
- **THEN** none of them is copied, the report before the copy counts each under its reason with bytes, and the manifest gives each a code

#### Scenario: A user opts back in

- **WHEN** the user passes `--include-hidden` or `--no-gitignore`
- **THEN** the entries of that reason are staged like the others, and the printed `start` path asks adoption to include hidden files when hidden entries were staged

#### Scenario: A file that cannot be copied keeps the import open

- **WHEN** a selected file changes while the command copies it
- **THEN** the file is not staged torn, the manifest gives it a code, the command exits with an error and names the rerun, and the same command run again copies only that file under the same import id

#### Scenario: A file the vault refuses does not hold the import open

- **WHEN** a selected file has a name that the vault never accepts
- **THEN** the manifest records the file as left out with the vault's code, the import finishes, the report counts the file, and the command prints the `start` path

#### Scenario: A file the disk refuses keeps the import open

- **WHEN** the disk refuses to store a selected file and the other files are staged
- **THEN** the manifest records the file as left out with a code, the command exits with an error and names the rerun, and the same command run again copies only that file under the same import id

#### Scenario: A resume removes what the folder no longer holds

- **WHEN** a file staged by an interrupted run is deleted from the folder before the rerun
- **THEN** the rerun removes the staged file, and the manifest records the removal

#### Scenario: A folder too large for the disk is refused before copying

- **WHEN** the folder's bytes exceed the vault filesystem's free space
- **THEN** the command refuses with the measured bytes and the free space, and copies nothing

#### Scenario: The command names the second copy

- **WHEN** staging finishes
- **THEN** the report gives the staged bytes, says that they duplicate the original until discarded, and names the discard route

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
