## MODIFIED Requirements

### Requirement: Deterministic read-only scan and candidate inventory

`adoption_studio(action="start")` SHALL perform a read-only `overview` scan of the target subtree and record, in the run object, a compact `scan_summary`, `pack_suggestions`, and a candidate `inventory` containing every adoption-relevant file outside `Knowledge Base/` with its vault-relative path, byte size, mtime, and import form (`text` for a text-like suffix, `artifact` for any other regular file). `start` SHALL accept an optional `collection` label and SHALL default it to the scanned folder's name, or to the single top-level folder inside an `_Imports/<import_id>/` stage. A skipped hidden folder SHALL be recorded as one inventory row with its file count and reason. Ineligible candidates SHALL be retained in the inventory with a machine-readable `reason` (never silently dropped), the inventory SHALL be capped at a configurable `max_candidates` with an explicit `inventory_truncated` counter, and a stat-based `inventory_fingerprint` SHALL be computed over the sorted eligible rows. The scan SHALL write nothing outside the run object. `start` SHALL require `Knowledge Base/` to exist and refuse with `KB_NOT_INITIALIZED` unless `initialize_kb=true`, in which case it initializes the scaffold first; pure pre-init discovery remains `adopt_vault(mode="scan-only")`, unchanged.

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

### Requirement: Server-materialized selection rules with per-path validation

`adoption_studio(action="select")` SHALL accept a folder-rule selection payload `{include, exclude, overrides, include_junk}` and materialize the concrete file set **server-side** so the client never enumerates thousands of files. An `include` folder rule MAY carry a `source_kind` and a `domain`; `select` SHALL resolve each through the vault's Source taxonomy registry, SHALL accept only a registered result, and SHALL refuse a rule whose key is unregistered or a near miss, with the resolver's close match, without changing the selection. Rule classification SHALL be part of the selection identity. Each materialized path SHALL record the classification of the deepest rule that selects it. Each materialized path SHALL be validated against the inventory and rejected per-path with a machine-readable code — `NOT_IN_INVENTORY`, `TOO_LARGE`, or `ALREADY_GOVERNED` — never silently dropped. Any selection change SHALL clear any existing plan and return the run to phase `selecting`. Inventory drill-in SHALL be available with offset paging so the UI can page bounded file lists.

#### Scenario: Folder rules materialize a validated file set

- **WHEN** `select` is called with `include` of a folder, an `exclude` of a subfolder, an `overrides` list, and `include_junk=false`
- **THEN** the server materializes the concrete selected paths from the depth-capped tree and records them
- **AND** any oversized, missing, or already-governed path is returned in a per-path rejection list with its code

#### Scenario: A deeper rule overrides its ancestor

- **WHEN** `select` is called with `exclude` of a folder and `include` of one of its subfolders (the UI's uncheck-parent, re-check-child gesture)
- **THEN** rules apply in ascending path depth so the subfolder's files stay selected while the rest of the excluded folder is dropped
- **AND** a file-path `exclude` is the deepest rule of all and wins over its folder's rule

#### Scenario: A folder rule classifies its files from the registry

- **WHEN** `select` includes one folder with a registered `source_kind` and `domain`, and another folder with no classification
- **THEN** the first folder's paths record that kind and domain, and the second folder's paths record no classification
- **AND** a rule naming an unregistered kind is refused with a close match, and the stored selection is unchanged

#### Scenario: Changing the selection invalidates the plan

- **WHEN** a run in phase `planned` receives a `select` call that changes the materialized set or a rule's classification
- **THEN** the stored plan is cleared and the phase returns to `selecting`

### Requirement: Exact-action preview bound by plan identity

`adoption_studio(action="plan")` SHALL read and sha256-hash each selected file at plan time, compute the exact deterministic target path, title, and frontmatter for each item, record any skipped items with a code, and persist a `plan` carrying a `plan_id` derived from the selection hash and the complete planned actions: each item's destination, classification, provenance fields, rendered link projection, and original hash. Destinations SHALL be allocated before links resolve and SHALL NOT change afterwards. A text item that is not valid UTF-8 SHALL plan as an artifact. A classified item's target SHALL be in its kind's Source directory (`Sources/<Kind>/<Domain>/`); an unclassified item's target SHALL be in `Sources/Imported/`. For a text item, `plan` SHALL read its wikilinks, embeds, and local Markdown links and images with the shared parsers, and SHALL resolve each target against the planned items and earlier imports by exact collection-relative path, then note-relative path, then unique basename, then unique front-matter alias, leaving an ambiguous or missing target unresolved, and SHALL record the resolved targets, the unresolved targets, the front-matter tags and aliases it carries, and any tag the vault's tag normalization rejects. `plan` SHALL check each item against earlier imports by `import_collection` and `collection_path` (falling back to `imported_from` for an import that lacks them), SHALL record `existing_ref` for an item whose original an earlier import holds with the same bytes, and SHALL record the latest earlier Source as `previous_import` for an item whose bytes changed. The plan SHALL report the total bytes that `apply` will write. When the remaining request budget falls below its margin, `plan` SHALL stop, return `progress` and a `continuation`, and continue on a call that passes that cursor. `plan` SHALL write nothing outside the run object (targets are previewed, not created). The preview a caller sees SHALL be exactly what a subsequent `apply` commits, or `apply` SHALL refuse.

#### Scenario: Plan previews exact targets without writing

- **WHEN** `plan` runs over a non-empty selection
- **THEN** each item records its `original_sha256`, exact `target_path`, `title`, and `frontmatter`, and a stable `plan_id` for identical inputs
- **AND** a byte snapshot of the vault before and after `plan` is identical outside the run object

#### Scenario: Replacement cannot redirect an original read

- **WHEN** a selected original or its parent is replaced with a link during planning or apply
- **THEN** retained no-follow custody prevents the read from following that link
- **AND** a changed binding produces a coded refusal before publication instead of importing bytes from the replacement

#### Scenario: Plan resolves links without guessing

- **WHEN** a planned note links to another planned note by path, embeds a planned image by basename, links to a third note through its front-matter alias, and links to a basename that two planned notes share
- **THEN** the first three targets are recorded as resolved to those items' frozen target paths
- **AND** the shared basename is recorded as unresolved

#### Scenario: Plan recognizes an earlier import

- **WHEN** a collection that an earlier run imported is staged again under a new location with the same `collection` label and one note changed
- **THEN** every unchanged item records the `existing_ref` of its earlier Source
- **AND** the changed note plans as a new item that names its earlier Source as `previous_import`

#### Scenario: Apply refuses a stale plan

- **WHEN** `apply` is called with a `plan_id` that does not equal the stored plan's id, or the current selection hash no longer matches the plan's selection hash
- **THEN** it is refused with `PLAN_STALE` and writes nothing

### Requirement: Governed apply with write-time re-validation

`adoption_studio(action="apply")` SHALL process the planned items that have no outcome yet, in plan order, in chunks bounded by item count and bytes. For each item it SHALL re-read the original bytes and re-verify the sha256 against the value captured at `plan` immediately before writing. Before each write it SHALL repeat the collection identity check under its mutation guard. Each chunk SHALL commit its text pages, streamed artifacts, outcomes, Sources index, top index and log update in one `batch_atomic_write`. Index and log updates SHALL use the actual destinations. Artifact writes SHALL use the Source artifact seam shared with capture and SHALL keep the original bytes. An extraction request SHALL follow each artifact commit. An item whose bytes changed SHALL be excluded with per-item outcome `SOURCE_CHANGED`; a missing item SHALL be excluded with `NOT_FOUND`; an item whose frozen destination another writer now occupies SHALL fail with `TARGET_OCCUPIED` and SHALL NOT move; an item with `existing_ref`, or one that the identity check finds already committed, SHALL record `already-applied` with that ref and write nothing. Applied Sources SHALL carry `imported_from`, `import_collection`, `collection_path`, and `original_sha256` provenance. A text Source SHALL carry the original text verbatim in its fenced capture, so that the captured text encodes to bytes whose sha256 equals `original_sha256`, and its resolved links in a generated `## Links` section outside that fence. When the remaining request budget falls below its margin, `apply` SHALL stop between commits and return `progress` (`applied`, `remaining`, `total`) and a `continuation`; an `apply` passing that cursor with the same `plan_id` SHALL continue, and an `apply` repeating an earlier cursor SHALL return that call's result again. The phase SHALL become `applied` only when every planned item has an outcome. The result SHALL include `verified_unchanged` and `verified_total` counts from a post-apply re-hash of the originals handled by that call. A `plan`/`apply` invalidated by inventory drift SHALL surface `ADOPTION_SOURCE_CHANGED` so the client can re-scan while preserving the still-valid selection.

#### Scenario: Apply copies with provenance and leaves originals byte-identical

- **WHEN** `apply` commits a validated plan
- **THEN** each imported Source carries `imported_from`, `import_collection`, `collection_path`, and `original_sha256`, the Sources index and log are updated with each call, and every original file is byte-identical before and after
- **AND** the result reports `verified_unchanged` equal to `verified_total`

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

### Requirement: Per-item outcomes, partial results, and failed-subset retry

The run SHALL record a per-path outcome (`applied`, `failed`, `skipped`, or `already-applied`) with a code for failures and, for `already-applied`, the ref of the Source that holds the original. When some items apply and others fail the phase SHALL be `partial`; when none apply it SHALL be `failed`. `apply(retry_failed=true, only_paths=[...])` SHALL re-plan and re-apply only the failed subset in place with fresh hashes, and re-applying an already-applied item SHALL be an idempotent `already-applied` skip. A failed text batch SHALL roll back only the items of that call, and a commit whose state is unknown SHALL be resolved by the identity check on the next call. The UI-facing retry SHALL be expressible as `scope:"failed"`.

#### Scenario: Retry re-applies only the failed subset

- **WHEN** a run is `partial` and the previously failed original is restored, then `apply` is called with `retry_failed=true`
- **THEN** only the failed subset is re-planned and applied, previously applied items are left untouched, and the phase becomes `applied`

#### Scenario: Re-applying an applied item is idempotent

- **WHEN** `apply` is re-run over a plan whose items already have `applied` outcomes
- **THEN** those items report `already-applied` and no duplicate Source is created

#### Scenario: A second run over the same collection writes nothing

- **WHEN** a new run selects, plans and applies a collection that an earlier run imported, unchanged
- **THEN** every item reports `already-applied` with its earlier Source's ref
- **AND** no Source page or artifact is created

### Requirement: Interrupted apply is visible and recoverable

The run SHALL persist phase `applying` before the first item write so a process death mid-apply is visible. `status` SHALL report `interrupted: true` when the phase is `applying` with incomplete outcomes and no call returned a budget stop, and SHALL report the remaining count and the last `continuation` when the last call returned a budget stop. A subsequent `apply` SHALL be safely re-runnable because already-applied detection and fresh write-time re-validation make re-entry safe.

#### Scenario: A crashed apply is reported and can resume

- **WHEN** the run is left in phase `applying` with incomplete outcomes
- **THEN** `status` reports `interrupted: true`
- **AND** calling `apply` again completes the remaining validated items without duplicating applied ones

#### Scenario: A budget stop is remaining work, not a crash

- **WHEN** the last `apply` returned a budget stop with items remaining
- **THEN** `status` reports the remaining count and the cursor to continue with, and does not report `interrupted: true`

### Requirement: First-question handoff on finish

`adoption_studio(action="finish")` SHALL be allowed from `applied` and `partial`, run a deterministic recall check (keyword `find` over the largest imported text title, or the largest artifact title when no text item exists, counting a hit only on the exact target path) producing `recall_check = {query, ok, hits}`, compute a `first_question` referencing that title with a ready-to-run `ask_memory` route, optionally write a run manifest under `Knowledge Base/_Adoption/`, and set phase `done`. `finish` SHALL also return `coverage`: outcome counts by status and code; skipped hidden folders and junk counted by reason with file counts; for artifact items, extraction states counted as done, pending, engine disabled, failed, or unknown; resolved and unresolved link counts; and a recall sample of up to ten applied items spread over the collection's top-level folders in a fixed order, counting a hit only on the exact target path, reported as found over sampled. A pending or unknown extraction SHALL never be reported as done. Open proposals SHALL NOT block finish; there is no engine timer or background thread. `status`/`finish` SHALL include a `handoff` block carrying `prompt_text` and deterministic `links`.

#### Scenario: Finish proves recall and offers the first question

- **WHEN** `finish` runs on a run with applied imports
- **THEN** `recall_check.ok` is true because an imported target appears in the keyword hits, `first_question` references the imported title with an `ask_memory` route, and the phase becomes `done`
- **AND** any run manifest is written only under `Knowledge Base/_Adoption/`

#### Scenario: Finish reports coverage honestly

- **WHEN** `finish` runs on a run whose applied items include an image waiting for extraction and a note with one unresolved link
- **THEN** `coverage` counts the image as pending and the link as unresolved
- **AND** the recall sample reports how many sampled items it found out of how many it sampled

### Requirement: Originals are never modified

Across every action and every failure mode, adoption SHALL never rewrite, move, or delete an original file. Imports are copies into `Knowledge Base/Sources/Imported/` or, for a classified item, into its kind's Source directory, carrying provenance; an artifact import copies the original bytes. Scans, selection, and preview write nothing outside the run object; cancellation, budget stops and partial/failed apply leave already-copied Sources intact and originals untouched.

#### Scenario: Originals are byte-identical after any outcome

- **WHEN** a run proceeds through scan, select, plan, a partially-failing apply, a budget stop, a retry, and a cancel attempt
- **THEN** every original file is byte-identical to its pre-run bytes at every step
- **AND** no original is moved or deleted by any action

## ADDED Requirements

### Requirement: Cloud staged imports adopt through the local engine

On Exomem Cloud, an `adopt` import SHALL land as RAW files under the vault-relative staging directory `_Imports/<import_id>/`, outside `Knowledge Base/` so the engine treats them as legacy input. The Cloud import job SHALL unpack and validate the archive before the folder appears there, with its member, path and size checks. `adoption_studio(action="start")` SHALL accept `path` pointing at that staging directory and scan it with the identical engine used locally; local runs SHALL never use staging.

#### Scenario: A staged Cloud import adopts through the local engine

- **WHEN** an `adopt` import has finished and its files are in `_Imports/<import_id>/`
- **THEN** `adoption_studio(start, path="_Imports/<import_id>")` scans the staged files through the same engine as a local run
- **AND** applying the run imports them as Sources without modifying or replacing existing notes

## REMOVED Requirements

### Requirement: Hosted staging intake for uploads and archives

**Reason**: The hosted platform that staged uploads and ZIP archives under `_Staging/adoption/<run_id>/` is retired; no code writes that folder. Exomem Cloud stages an `adopt` import under `_Imports/<import_id>/` through its import job.

**Migration**: Use the Cloud import route (`exomem cloud import` or Home), then run `adoption_studio(start, path="_Imports/<import_id>")`. The new requirement "Cloud staged imports adopt through the local engine" states the contract.
