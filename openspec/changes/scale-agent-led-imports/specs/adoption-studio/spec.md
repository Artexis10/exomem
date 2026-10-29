## ADDED Requirements

### Requirement: Recorded staged artifacts are consumed after verified filing

A run SHALL consume only artifact items whose staged copies lie in a recorded import stage `_Imports/<import_id>/`. A recorded stage has a manifest that lists each staged file's stage-relative path, size and SHA-256: the Cloud import receipt's member manifest for an `adopt` import, or the stage record that `exomem import` writes when an import finishes. A run started with `consume=false` SHALL never consume. Text items SHALL never be consumed.

After the chunk that files an artifact item commits, `apply` SHALL remove its staged copy only when all of these hold:

- the outcome is `applied` or `already-applied`;
- the durable artifact matches the planned `original_sha256`;
- the staged file's path, size and SHA-256 match a manifest entry.

The removal SHALL go through the held filesystem with an identity check. A removal that fails SHALL leave the item applied and not consumed.

When an artifact item's staged copy is missing in a recorded stage, `apply` SHALL resolve the item through the import index, by collection, `collection_path` and `original_sha256`, before it reports a failure. Repeat-call re-verification and staleness probes SHALL treat a missing staged copy whose Source verifies as consumed. The run SHALL report a `consumed` count separately from `verified_unchanged`. `finish` SHALL remove empty folders left in the stage, SHALL report the residual still in the stage (unselected, ineligible, failed and text items) with counts and bytes, and SHALL name the discard route.

#### Scenario: Filing a recorded stage keeps about one copy of its artifacts

- **WHEN** a run over a recorded stage applies every selected artifact
- **THEN** each applied artifact's staged copy is gone, its Knowledge Base copy matches the planned hash, and `consumed` counts them
- **AND** `finish` reports the residual's counts and bytes and names the discard route

#### Scenario: Nothing outside a manifest is consumed

- **WHEN** a run applies artifacts from a folder under `_Imports/` that has no manifest, or a file added to a recorded stage after its manifest was written
- **THEN** those files are byte-identical afterwards

#### Scenario: A stop between commit and removal recovers from the Source

- **WHEN** the process stops after a chunk commits and before the run object records its outcomes or removes the staged copies
- **THEN** the next `apply` resolves each item whose staged copy is gone through its committed Source, removes any listed staged copy still present, and reports no failure

#### Scenario: Two runs over one stage do not fail each other

- **WHEN** a second run applies an item whose staged copy the first run consumed
- **THEN** the second run resolves it as already applied through the import index

#### Scenario: Text and opted-out runs keep their staged copies

- **WHEN** a run files text items, or a run over a recorded stage started with `consume=false` files artifacts
- **THEN** those staged copies remain after apply

### Requirement: Apply bounds space, memory and time per chunk

Before it writes a chunk, `apply` SHALL compare the chunk's bytes plus the vault's free-space reserve against the free space of the vault's filesystem. When they do not fit, it SHALL stop the call before writing that chunk with a resumable `INSUFFICIENT_SPACE` outcome that gives the measured bytes and the free space, and a next action. Apply SHALL copy an artifact in one streamed pass that hashes while it copies and SHALL roll the item back on a mismatch, so memory does not grow with the file's size. A staged artifact above the adoption ceiling SHALL stay in the residual with a code that names the ceiling.

#### Scenario: A chunk that does not fit stops the call cleanly

- **WHEN** the next chunk's bytes plus the reserve exceed the free space
- **THEN** the call stops before writing it, reports `INSUFFICIENT_SPACE` with the measured bytes, and a later `apply` continues from that chunk

#### Scenario: A large staged artifact adopts on Cloud

- **WHEN** a Cloud cell applies a staged artifact over 100 MiB and within the adoption ceiling
- **THEN** its Knowledge Base copy matches the planned hash, and the process's memory does not grow with the file's size

### Requirement: Local staging records a finished stage in the vault

When an `exomem import` finishes, the command SHALL write a stage record for the import id in the vault's state folder. The record SHALL list each staged file's stage-relative path, size and SHA-256 from the journal. An import that stays open SHALL have no record. When the vault's state placement is not yet settled, the command SHALL write no record and SHALL report that adoption will keep the staged copy until discard.

#### Scenario: A finished local stage qualifies for consuming

- **WHEN** `exomem import` finishes staging a folder into an initialized vault and adoption runs on the printed path
- **THEN** applied artifacts' staged copies are consumed

#### Scenario: An open local import is not consumed

- **WHEN** adoption runs on the path of a local import that stays open
- **THEN** no staged copy is consumed, and a rerun of the import copies only what was missing

## MODIFIED Requirements

### Requirement: Deterministic read-only scan and candidate inventory

`adoption_studio(action="start")` SHALL perform a read-only `overview` scan of the target subtree and record, in the run object, a compact `scan_summary`, `pack_suggestions`, and a candidate `inventory` containing every adoption-relevant file outside `Knowledge Base/` with its vault-relative path, byte size, mtime, and import form (`text` for a text-like suffix, `artifact` for any other regular file). `start` SHALL accept an optional `collection` label and SHALL default it to the scanned folder's name, or to the single top-level folder inside an `_Imports/<import_id>/` stage. A skipped hidden folder SHALL be recorded as one inventory row with its file count and reason. Ineligible candidates SHALL be retained in the inventory with a machine-readable `reason` (never silently dropped), the inventory SHALL be capped at a configurable `max_candidates` with an explicit `inventory_truncated` counter, `start` SHALL return an opaque `next_after` cursor while rows remain beyond the cap and SHALL accept it as `after`, recording only the rows that follow it under the collection root and label pinned by the first page, with artifact rows ordered before text rows, and a stat-based `inventory_fingerprint` SHALL be computed over the sorted eligible rows. The scan SHALL write nothing outside the run object. `start` SHALL require `Knowledge Base/` to exist and refuse with `KB_NOT_INITIALIZED` unless `initialize_kb=true`, in which case it initializes the scaffold first; pure pre-init discovery remains `adopt_vault(mode="scan-only")`, unchanged.

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
- **THEN** `start` records the first page and returns `next_after`
- **AND** each `start` with the previous `next_after` records the next page under the same collection root and label, so the pages cover every file exactly once

#### Scenario: Consuming does not change a later page's collection identity

- **WHEN** applying the first page consumes a top-level staged file and leaves a folder empty
- **THEN** the next page keeps the first page's collection root, label and `collection_path` values

#### Scenario: A paged collection never guesses a link target

- **WHEN** a note on an early page links by a name that only an alias on a later page could match
- **THEN** the link stays unresolved instead of resolving to another target
- **AND** a link whose bare name is unique across the whole scanned folder resolves

#### Scenario: A page waits for the one before it

- **WHEN** `plan` is called for a page while an earlier page of the same collection still has unapplied plan items
- **THEN** it refuses and names the earlier run

#### Scenario: A staged artifact over the client-pull cap is eligible on every runtime

- **WHEN** a Cloud cell scans a staged artifact larger than the gateway's client-pull cap and within the adoption ceiling
- **THEN** the row is eligible with import form `artifact`, as it is on a standalone server

### Requirement: Originals are never modified

Across every action and every failure mode, adoption SHALL never rewrite, move, or delete an original file. A staged artifact listed in a recorded import stage's manifest is not an original: the person's original stays on their own device or folder, and adoption MAY remove that staged copy only as the requirement on consuming recorded staged copies allows. Imports are copies into `Knowledge Base/Sources/Imported/` or, for a classified item, into its kind's Source directory, carrying provenance; an artifact import copies the original bytes. Scans, selection, and preview write nothing outside the run object; cancellation, budget stops and partial/failed apply leave already-copied Sources intact and originals untouched.

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

When the import finishes, the command SHALL end by reporting the bytes it staged, stating that the staged folder is a second copy of the data on the disk until adoption consumes each staged artifact or the folder is discarded, naming the discard route that exists, and printing the `adoption_studio start` path. An import that stays open SHALL also print that path, as the way to adopt what is staged so far.

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
- **THEN** the report gives the staged bytes, says that they duplicate the original until adoption consumes the staged artifacts or they are discarded, and names the discard route

#### Scenario: An outside path given to start is refused with the route

- **WHEN** `adoption_studio start` receives the absolute path of a folder outside the vault
- **THEN** it refuses with `IMPORT_PATH_OUTSIDE_VAULT` and names `exomem import`
