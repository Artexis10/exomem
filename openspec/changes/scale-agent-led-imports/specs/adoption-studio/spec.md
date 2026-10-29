## ADDED Requirements

### Requirement: Recorded staged copies are consumed after verified filing

A run SHALL consume staged copies only when its scanned folder lies inside a recorded import stage `_Imports/<import_id>/`. A Cloud stage is recorded by the import's vault-state receipt for an `adopt` import. A local stage is recorded by the stage record that `exomem import` writes in the vault's state folder. `start` SHALL record the stage and its proof in the run object, and a run started with `consume=false` SHALL never consume.

For a recorded stage, after the chunk that files an item commits, `apply` SHALL remove the item's staged copy only when all of these hold:

- the item's outcome is `applied` or `already-applied`, with no draft note still owed;
- the durable copy reproduces the planned `original_sha256` when read back after the commit;
- the staged copy still has the planned size and hash.

The outcome SHALL record `consume: "pending"` in the run object that the commit writes, before the removal. On replay, a pending item whose staged copy is missing and whose durable copy verifies SHALL count as consumed. Repeat-call re-verification and staleness probes SHALL skip consumed items, and the item SHALL keep the `original_unchanged` result taken before the removal. `finish` SHALL remove empty folders left in the stage. It SHALL report the residual still in the stage (unselected, ineligible and failed items), with counts and bytes, and name the discard route.

#### Scenario: Filing a recorded stage keeps about one copy on disk

- **WHEN** a run over a recorded stage applies every selected item
- **THEN** each applied item's staged copy is gone, its Source or artifact verifies against the planned hash, and the stage holds only the residual
- **AND** `finish` reports the residual's counts and bytes and names the discard route

#### Scenario: A folder without a stage record is never consumed

- **WHEN** a run applies items from a folder under `_Imports/` that has no receipt or stage record, or from any other folder
- **THEN** every original is byte-identical afterwards

#### Scenario: An item still owed a draft note keeps its staged copy

- **WHEN** an item's Source is committed and its draft note is still owed
- **THEN** its staged copy stays until the note settles, and is consumed in the call that settles it

#### Scenario: A crash between commit and removal recovers

- **WHEN** the process stops after a chunk commits and before its staged copies are removed
- **THEN** the next `apply` verifies each pending item's durable copy, removes any staged copy still present, and reports no failure for the missing ones

#### Scenario: An opted-out run consumes nothing

- **WHEN** a run over a recorded stage started with `consume=false`
- **THEN** every staged copy remains after apply

### Requirement: Apply checks free space before each chunk

Before it writes a chunk, `apply` SHALL compare the chunk's bytes plus the vault's free-space reserve against the free space of the vault's filesystem. When they do not fit, it SHALL stop the call before writing that chunk with a resumable `INSUFFICIENT_SPACE` outcome that gives the measured bytes and the free space, and a next action. Apply SHALL read, hash and copy an artifact in bounded chunks, so that memory does not grow with the file's size.

#### Scenario: A chunk that does not fit stops the call cleanly

- **WHEN** the next chunk's bytes plus the reserve exceed the free space
- **THEN** the call stops before writing it, reports `INSUFFICIENT_SPACE` with the measured bytes, and a later `apply` continues from that chunk

### Requirement: Local staging records its stage in the vault

When `exomem import` claims an import id, it SHALL write a stage record for that id in the vault's state folder, holding the import id and the staged folder's name. When the vault's state placement is not yet settled, it SHALL write no record and SHALL report that adoption will keep the staged copy until discard.

#### Scenario: A local stage qualifies for consuming

- **WHEN** `exomem import` stages a folder into an initialized vault and adoption runs on the printed path
- **THEN** the run records the stage as recorded, and applied items' staged copies are consumed

## MODIFIED Requirements

### Requirement: Deterministic read-only scan and candidate inventory

`adoption_studio(action="start")` SHALL perform a read-only `overview` scan of the target subtree and record, in the run object, a compact `scan_summary`, `pack_suggestions`, and a candidate `inventory` containing every adoption-relevant file outside `Knowledge Base/` with its vault-relative path, byte size, mtime, and import form (`text` for a text-like suffix, `artifact` for any other regular file). `start` SHALL accept an optional `collection` label and SHALL default it to the scanned folder's name, or to the single top-level folder inside an `_Imports/<import_id>/` stage. A skipped hidden folder SHALL be recorded as one inventory row with its file count and reason. Ineligible candidates SHALL be retained in the inventory with a machine-readable `reason` (never silently dropped), the inventory SHALL be capped at a configurable `max_candidates` with an explicit `inventory_truncated` counter, `start` SHALL accept an optional `after` cursor (a vault-relative path) and record only rows that sort strictly after it in the scan's deterministic order, and it SHALL return `next_after`, the last recorded path, while rows remain beyond the cap, and a stat-based `inventory_fingerprint` SHALL be computed over the sorted eligible rows. The scan SHALL write nothing outside the run object. `start` SHALL require `Knowledge Base/` to exist and refuse with `KB_NOT_INITIALIZED` unless `initialize_kb=true`, in which case it initializes the scaffold first; pure pre-init discovery remains `adopt_vault(mode="scan-only")`, unchanged.

#### Scenario: Scan snapshots an eligible-only inventory deterministically

- **WHEN** `start` runs twice against an unchanged subtree
- **THEN** both runs record the same eligible candidate rows in the same order and the same `inventory_fingerprint`
- **AND** ineligible files (for example already-governed paths or files over the artifact ceiling) appear in the inventory with a `reason` rather than being omitted
- **AND** no file outside the run object is created or modified

#### Scenario: Non-text files are eligible as artifacts

- **WHEN** `start` scans a folder holding Markdown notes, a PDF, a PNG image, an audio file and a video file
- **THEN** every one of them is an eligible inventory row
- **AND** the notes have import form `text` and the other files have import form `artifact`

#### Scenario: Start refuses an uninitialized vault unless asked to bootstrap

- **WHEN** `start` is called and `Knowledge Base/` does not exist
- **THEN** it is refused with `KB_NOT_INITIALIZED` and writes nothing
- **AND** calling `start` with `initialize_kb=true` first initializes the scaffold and then records the run


#### Scenario: A folder with more files than the cap is covered page by page

- **WHEN** a staged folder holds more eligible files than `max_candidates` in one flat folder
- **THEN** `start` records the first `max_candidates` rows and returns `next_after`
- **AND** `start` with `after` set to that value records the next rows, so successive pages cover every file exactly once and in the same order

#### Scenario: A staged file over the client-pull cap is eligible on every runtime

- **WHEN** a Cloud cell scans a staged artifact larger than the gateway's client-pull cap and within the upload-session cap
- **THEN** the row is eligible with import form `artifact`, as it is on a standalone server

### Requirement: Originals are never modified

Across every action and every failure mode, adoption SHALL never rewrite, move, or delete an original file. A staged copy inside a recorded import stage is not an original: the person's original stays on their own device or folder, and adoption MAY remove that staged copy only as the requirement on consuming recorded staged copies allows. Imports are copies into `Knowledge Base/Sources/Imported/` or, for a classified item, into its kind's Source directory, carrying provenance; an artifact import copies the original bytes. Scans, selection, and preview write nothing outside the run object; cancellation, budget stops and partial/failed apply leave already-copied Sources intact and originals untouched.

#### Scenario: Originals are byte-identical after any outcome

- **WHEN** a run proceeds through scan, select, plan, a partially-failing apply, a budget stop, a retry, and a cancel attempt
- **AND** the folder is not a recorded import stage
- **THEN** every original file is byte-identical to its pre-run bytes at every step
- **AND** no original is moved or deleted by any action

### Requirement: A folder outside the vault imports through local staging

`adoption_studio start` SHALL refuse a `path` that is absolute, or that climbs out of the vault by `..`, with `IMPORT_PATH_OUTSIDE_VAULT`, and the message SHALL name `exomem import <folder>`. The check SHALL read the path as text and SHALL NOT resolve it on the filesystem. A path that escapes through a symbolic link inside the vault SHALL get the overview resolver's `INVALID_PATH`.

The `exomem import <folder>` command SHALL enumerate the folder without following symbolic links, and SHALL select files by these rules:

- A hidden entry (a name that starts with a dot, or the Windows hidden attribute), at any depth, SHALL be left behind unless the user passes `--include-hidden`.
- The `.gitignore` files inside the folder SHALL apply, with git's precedence, unless the user passes `--no-gitignore`. The command SHALL NOT hard-code a folder name to leave behind.
- A symbolic link or a Windows junction at any depth SHALL be left behind and never followed. A special file and an unreadable file SHALL be left behind with their own reasons.

Before it copies, the command SHALL report what it leaves behind, with the count and bytes under each reason, and SHALL report the selected file count and bytes against the free space of the vault's filesystem. It SHALL refuse before copying, with the measured bytes and the free space, when the copy plus a reserve does not fit. The limits the adoption engine applies after staging SHALL be warnings, not refusals. Otherwise it SHALL copy each selected regular file into `_Imports/<import_id>/<folder name>/`, streaming, record a manifest of relative path, size and SHA-256, re-hash every copy, and report progress while copying. Every entry left behind SHALL get a per-path code in the manifest. The command SHALL never modify the original folder, and the folder's name SHALL be the collection label. A file that changed while it was read, could not be read, or was refused by the disk SHALL be left out with a code and SHALL keep the import open: the command SHALL exit with an error and say that the same command run again retries only those files under the same import id. A file that the vault refuses because of its own name or path SHALL be left out with the vault's code and SHALL NOT keep the import open: the import SHALL finish, and the report SHALL give the count of such files. An interrupted or incomplete copy SHALL resume from its manifest under the same import id without copying a verified file again. On a resume, a staged file that the folder no longer selects SHALL be removed and recorded in the manifest, and a staged file that changed after it was verified SHALL be copied again. The command SHALL name an unfinished import made with other options and SHALL NOT resume it, and SHALL NOT let two runs write the same import at once.

When the import finishes, the command SHALL end by reporting the bytes it staged, stating that the staged folder is a second copy of the data on the disk until adoption files and consumes each staged file or the folder is discarded, naming the discard route that exists, and printing the `adoption_studio start` path. An import that stays open SHALL also print that path, as the way to adopt what is staged so far.

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
- **THEN** the report gives the staged bytes, says that they duplicate the original until adoption consumes them or they are discarded, and names the discard route

#### Scenario: An outside path given to start is refused with the route

- **WHEN** `adoption_studio start` receives the absolute path of a folder outside the vault
- **THEN** it refuses with `IMPORT_PATH_OUTSIDE_VAULT` and names `exomem import`
