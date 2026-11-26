## ADDED Requirements

### Requirement: Recorded staged files are consumed after verified filing

A run SHALL consume only staged copies that lie in a recorded import stage `_Imports/<import_id>/`. A recorded stage has a manifest that lists each staged file's stage-relative path, size and SHA-256: the Cloud import receipt's member manifest for an `adopt` import, or the stage record that `exomem import` writes when an import finishes. A run started with `consume=false` SHALL never consume. `apply` SHALL consume artifact items. `finish` SHALL consume text items.

After the chunk that files an artifact item commits, `apply` SHALL remove its staged copy only when all of these hold:

- the outcome is `applied` or `already-applied`;
- the durable artifact matches the planned `original_sha256`;
- the staged file's path, size and SHA-256 match a manifest entry.

The removal SHALL go through the held filesystem with an identity check. A removal that fails SHALL leave the item applied and not consumed.

When an artifact item's staged copy is missing in a recorded stage, `apply` SHALL resolve the item through the import index, by collection, `collection_path` and `original_sha256`, before it reports a failure. Repeat-call re-verification and staleness probes SHALL treat a missing staged copy whose Source verifies as consumed. The run SHALL report a `consumed` count separately from `verified_unchanged`. `finish` SHALL remove empty folders left in the stage.

`finish` SHALL remove the staged copy of a text item only when all of these hold:

- the outcome is `applied` or `already-applied`, so an item that still owes its note keeps its copy;
- the run did not start with `consume=false`, and the stage is recorded;
- the Source page holds the text between its fences, byte for byte, as the plan's `original_sha256` and `original_bytes` describe;
- the staged file still matches the manifest entry and the plan.

A page of a paged collection SHALL remove only the text that it filed. When the call budget runs low, `finish` SHALL stop starting removals; the copies that remain are `unchecked`, not yet verified against their Source pages, and count as unfiled. A finished run takes no second `finish`.

When a text item's staged copy is missing in a recorded stage, the run SHALL treat the item as consumed when the run recorded the item as `applied` or `already-applied` and its Source page verifies in the same way. An item that still owes its note SHALL stay open and read as stale.

`finish` SHALL report the residual still in the stage with counts and bytes. The residual SHALL separate the files that adoption did not file or `finish` did not verify (`unfiled`, with its files and bytes) from the filed copies that remain. It SHALL carry a `summary` sentence that names the files that need review and the reason for each group. It SHALL name the discard route. While any file remains, it SHALL offer the review and a discard that needs the person's agreement, and it SHALL NOT offer a discard as a plain next action, because `finish` does not verify every filed copy that remains.

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

#### Scenario: Finish removes the filed text it verified

- **WHEN** a run files text items from a recorded stage and `finish` runs
- **THEN** each filed text item's staged copy is gone, and `consumed` counts it
- **AND** the stage holds no file, the residual lists none, and `finish` offers no review

#### Scenario: Finish keeps a text copy it cannot vouch for

- **WHEN** the Source page of a filed text item no longer holds the planned text between its fences, or the item still owes its note, or the item failed
- **THEN** its staged copy remains, and the residual reports it as unfiled
- **AND** a text item whose Source page no longer holds its text is reported as `source_differs`
- **AND** a text item whose Source page holds its text but whose staged copy changed after the plan is reported as `copy_changed`

#### Scenario: Finish reports the copies it did not reach as not yet verified

- **WHEN** `finish` runs out of its call budget before it checks every filed text copy
- **THEN** the copies it did not reach stay staged and the residual reports them as `unchecked`, not yet verified against their Source pages
- **AND** `finish` offers the review and a discard that needs the person's agreement, and no further `finish`

#### Scenario: Every file left needs the person before a discard

- **WHEN** every file left in the stage is one that the residual counts as filed, such as the staged copies of a run that started with `consume=false`
- **THEN** `finish` offers the review, and the discard states that it needs the person's agreement

#### Scenario: Opted-out runs keep their staged copies

- **WHEN** a run over a recorded stage started with `consume=false` files artifacts or text
- **THEN** those staged copies remain after apply and after finish

#### Scenario: A stop between a removal and the save recovers from the Source

- **WHEN** `finish` stops after it removes a text copy and before it saves the run
- **THEN** a repeat call reads the item as consumed through its verified Source, not as changed or missing

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

### Requirement: A staged import is not indexed

The vault-wide scans that build search, recall and their catalogues SHALL skip each stage, `_Imports/<import_id>/` at the vault root. A staged file SHALL reach the indexes only through the Source that adoption files for it. A folder named `_Imports` elsewhere, or a named folder under the root `_Imports/`, SHALL stay indexed. Adoption Studio SHALL still scan a stage that a run names.

#### Scenario: A staged note is not found by vault-scope search

- **WHEN** a note sits under `_Imports/<import_id>/` and a vault-scope search queries a term that only that note holds
- **THEN** the search returns no hit for it

#### Scenario: A person's own `_Imports` folder stays searchable

- **WHEN** a note sits in a folder named `_Imports` below another folder, or in a named folder under the root `_Imports/`
- **THEN** a vault-scope search finds it

#### Scenario: A large import does not hold the cell's readiness

- **WHEN** a Cloud import lands tens of thousands of staged notes while the runtime is stopped, and the runtime starts again
- **THEN** readiness does not wait on indexing the staged notes

## MODIFIED Requirements

### Requirement: Deterministic read-only scan and candidate inventory

`adoption_studio(action="start")` SHALL perform a read-only `overview` scan of the target subtree, except on a later page, and record, in the run object, a compact `scan_summary`, `pack_suggestions`, and a candidate `inventory` containing every adoption-relevant file outside `Knowledge Base/` with its vault-relative path, byte size, mtime, and import form (`text` for a text-like suffix, `artifact` for any other regular file). `start` SHALL accept an optional `collection` label and SHALL default it to the scanned folder's name, or to the single top-level folder inside an `_Imports/<import_id>/` stage. A skipped hidden folder SHALL be recorded as one inventory row with its file count and reason. Ineligible candidates SHALL be retained in the inventory with a machine-readable `reason` (never silently dropped), the inventory SHALL be capped at a configurable `max_candidates` with an explicit `inventory_truncated` counter, `start` SHALL return an opaque `next_after` cursor while rows remain beyond the cap and SHALL accept it as `after`, recording only the rows that follow it under the collection root and label pinned by the first page, with artifact rows ordered before text rows, and a stat-based `inventory_fingerprint` SHALL be computed over the sorted eligible rows. A later page's `start` SHALL carry the `scan_summary` and `pack_suggestions` of the page before it, which describe the collection as its first page scanned it, and SHALL list only its own rows. The scan SHALL write nothing outside the run object. `start` SHALL require `Knowledge Base/` to exist and refuse with `KB_NOT_INITIALIZED` unless `initialize_kb=true`, in which case it initializes the scaffold first; pure pre-init discovery remains `adopt_vault(mode="scan-only")`, unchanged.

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

### Requirement: Governed apply with write-time re-validation

`adoption_studio(action="apply")` SHALL process the planned items that have no outcome yet, in plan order, in chunks bounded by item count and bytes. For each item it SHALL re-read the original bytes and re-verify the sha256 against the value captured at `plan` immediately before writing. Before each write it SHALL repeat the collection identity check and the duplicate lookup under its mutation guard. Each chunk SHALL commit its text pages, streamed artifacts and outcomes in one `batch_atomic_write`, and, outside crash recovery, an item planned as `duplicate_of` that falls back to storing its own copy SHALL commit in a `batch_atomic_write` after the chunk's other items. The Sources index, the top index and the log SHALL record a page's applied items once per call, not once per chunk: the first chunk batch of an `apply` call SHALL record the items that earlier calls applied, the batch that completes the page SHALL record the rest, an `apply` call that ends with the page no longer `applying` SHALL record any items still owed, so that a page whose last chunk files nothing still records its items, and `finish` and `cancel` SHALL record any items still owed. A `cancel` whose recording fails SHALL warn and keep the items owed instead of failing. Each recording SHALL add one log entry that counts the items it records and does not list them, and the entry's provenance SHALL carry that count as `files`. A stop inside or between these batches SHALL NOT record an applied item twice: a recording SHALL name the items it carries, and settling it SHALL drop exactly those items. When the log cannot be read, the call SHALL stop with `IMPORT_RECOVERY_UNRESOLVED` and keep the items owed. Index and log updates SHALL use the actual destinations. Artifact writes SHALL use the Source artifact seam shared with capture and SHALL keep the original bytes. An extraction request SHALL follow each artifact commit. An item whose bytes changed SHALL be excluded with per-item outcome `SOURCE_CHANGED`; a missing item SHALL be excluded with `NOT_FOUND`; an item whose frozen destination another writer now occupies SHALL fail with `TARGET_OCCUPIED` and SHALL NOT move; an item with `existing_ref`, or one that the identity check finds already committed, SHALL record `already-applied` with that ref and write no Source and no artifact. Where such an item's own `import_collection` and `collection_path` are not yet recorded on that Source, `apply` SHALL add one import mapping to that Source's frontmatter under the mutation guard, guarded by the page's content hash and leaving its captured text and original bytes unchanged, and SHALL mark the outcome `mapped: true`. Where they are recorded, it SHALL write nothing. An item planned as `duplicate_of` another item SHALL do the same against that item's Source once that item has committed, and SHALL record `already-applied` with that Source's ref. For an item planned with an import mapping or as a `duplicate_of`, `apply` SHALL confirm the Source that the plan named, its `existing_ref` or the Source of the item it repeats, by reading that Source's stored bytes again, without the duplicate index. When that Source is missing, no longer holds the bytes or cannot be written by the caller, and the duplicate lookup finds no other Source that holds them and that the caller can write, `apply` SHALL store the item's own bytes as an ordinary item and mark its outcome `stored_own_copy`, and SHALL NOT fail it as `PLAN_STALE`. A re-run item of an unchanged path, which the plan found by its earlier import and planned with no mapping, SHALL still fail `PLAN_STALE` when its earlier Source page or artifact is missing or the page records a different hash for that path, so that a re-plan sees the current vault. Applied Sources SHALL carry `imported_from`, `import_collection`, `collection_path`, and `original_sha256` provenance for the first path that stored them, and one `also_imported_from` entry (`imported_from`, `import_collection`, `collection_path`) for each further path, so that every source path is recorded. A text Source SHALL carry the original text verbatim in its fenced capture, so that the captured text encodes to bytes whose sha256 equals `original_sha256`, and its resolved links in a generated `## Links` section outside that fence. When the remaining request budget falls below its margin, `apply` SHALL stop between commits and return `progress` (`applied`, `remaining`, `total`) and a `continuation`; an `apply` passing that cursor with the same `plan_id` SHALL continue, and an `apply` repeating an earlier cursor SHALL return that call's result again. The phase SHALL become `applied` only when every planned item has an outcome. The result SHALL include `verified_unchanged` and `verified_total` counts from a post-apply re-hash of the originals handled by that call. The result SHALL include `skipped_duplicates`: the count of the run's items that it did not store because a Source already held their bytes, whether an earlier item of the plan or an earlier run stored them. The default reply of `apply` SHALL carry it as `skipped_duplicates`, and `response_detail="full"` SHALL also hold it under `diagnostics.apply_result`. A `plan`/`apply` invalidated by inventory drift SHALL surface `ADOPTION_SOURCE_CHANGED` so the client can re-scan while preserving the still-valid selection.

#### Scenario: Apply copies with provenance and leaves originals byte-identical

- **WHEN** `apply` commits a validated plan
- **THEN** each imported Source carries `imported_from`, `import_collection`, `collection_path`, and `original_sha256`, the Sources index records every applied item and the log entries count them, with each entry's `files` provenance, once the call that completes the page has ended, and every original file is byte-identical before and after
- **AND** the result reports `verified_unchanged` equal to `verified_total`

#### Scenario: A call records its chunks in one log entry

- **WHEN** an `apply` call commits several chunks of a page and the page stays incomplete
- **THEN** the log gains no entry per chunk, and the next `apply` call's first chunk records that call's items in one entry that counts them

#### Scenario: A page whose last chunk files nothing still records its items

- **WHEN** an `apply` call files a page's first chunk and the page's last chunk holds only duplicates or failures
- **THEN** the page ends `applied` with no item owed, the Sources index records the filed items, and one log entry counts them

#### Scenario: A stop during filing records each item once

- **WHEN** the process stops between two chunks of an `apply` call, or inside the batch that records earlier calls' items, and later calls complete the page and `finish` runs
- **THEN** the Sources index counts each applied item once, and the log entries count each applied item once

#### Scenario: Attachments and links land together

- **WHEN** `apply` commits a plan holding a note that embeds a planned PDF and links to another planned note
- **THEN** the PDF is a Source whose artifact bytes equal the original
- **AND** the note's Source holds the original text verbatim in its fenced capture and a `## Links` section with links to the PDF's Source and the other note's Source
- **AND** an extraction request exists for the PDF

#### Scenario: A large plan finishes over several calls

- **WHEN** `apply` reaches its request budget with items remaining
- **THEN** it returns `progress` with a nonzero `remaining` and a `continuation`
- **AND** calls that pass each returned cursor commit only the remaining items, each item once, until `remaining` is zero and the phase is `applied`
- **AND** repeating an earlier cursor returns that call's result again and writes nothing

#### Scenario: A source changed after plan is excluded, not fatal

- **WHEN** one selected original is modified between `plan` and `apply` and others are unchanged
- **THEN** the changed item records outcome `SOURCE_CHANGED`, the unchanged items are applied, and the run phase becomes `partial`

#### Scenario: Identical photos import as one original with both paths

- **WHEN** `apply` commits a plan whose two photos have identical bytes
- **THEN** one Source and one artifact exist, byte-identical to the photo
- **AND** the Source records the second photo's path in `also_imported_from`
- **AND** the second item's outcome is `already-applied` with `mapped: true` and the first item's ref
- **AND** the `apply` reply reports `skipped_duplicates` of 1

#### Scenario: Apply needs no duplicate index for a Source the plan named

- **WHEN** `apply` commits a plan with two identical photos while the duplicate index is not built
- **THEN** the second item's outcome is `already-applied` with `mapped: true` and the first item's ref, and the run reaches `applied`
- **AND** when the first photo changed after the plan, the second item is stored as a Source of its own with `stored_own_copy` in its outcome, not failed as `PLAN_STALE`

#### Scenario: A plan warning reaches the reply's warning count

- **WHEN** a collection that an earlier run imported from one root is planned from another root
- **THEN** the plan reply and the apply reply each carry that warning in `warnings`
- **AND** `diagnostics.operational.warnings_count` counts it under `response_detail="full"`

#### Scenario: A second run writes nothing

- **WHEN** the same tree is planned and applied again unchanged
- **THEN** every item reports `already-applied` and no file in the vault changes

#### Scenario: The staging root moves

- **WHEN** the same collection is staged under a new root and imported again with the same collection label
- **THEN** every item matches by collection path and the run writes nothing

#### Scenario: A file is renamed inside the collection

- **WHEN** one file of an imported collection is renamed and the collection is imported again
- **THEN** the renamed item reports `already-applied` and adds one import mapping for its new collection path
- **AND** the next run writes nothing

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
