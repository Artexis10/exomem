## MODIFIED Requirements

### Requirement: Exact-action preview bound by plan identity

`adoption_studio(action="plan")` SHALL read and sha256-hash each selected file at plan time, compute the exact deterministic target path, title, and frontmatter for each item, record any skipped items with a code, and persist a `plan` carrying a `plan_id` derived from the selection hash and the complete planned actions: each item's destination, classification, provenance fields, rendered link projection, and original hash. Destinations SHALL be allocated before links resolve and SHALL NOT change afterwards. A text item that is not valid UTF-8 SHALL plan as an artifact. A classified item's target SHALL be in its kind's Source directory (`Sources/<Kind>/<Domain>/`); an unclassified item's target SHALL be in `Sources/Imported/`. For a text item, `plan` SHALL read its wikilinks, embeds, and local Markdown links and images with the shared parsers, and SHALL resolve each target against the planned items and earlier imports by exact collection-relative path, then note-relative path, then unique basename, then unique front-matter alias, leaving an ambiguous or missing target unresolved, and SHALL record the resolved targets, the unresolved targets, the aliases it carries, its tags, and any tag the vault's tag normalization rejects. A text item's tags SHALL be its front-matter tags followed by the inline `#tags` of its body that the shared Markdown parser reads outside code, comments and escapes; a hash inside code SHALL NOT become a tag. `plan` SHALL check each item against earlier imports by `import_collection` and `collection_path` (falling back to `imported_from` for an import that lacks them), SHALL record `existing_ref` for an item whose original an earlier import holds with the same bytes, and SHALL record the latest earlier Source as `previous_import` for an item whose bytes changed. The plan SHALL report the total bytes that `apply` will write. When the remaining request budget falls below its margin, `plan` SHALL stop, return `progress` and a `continuation`, and continue on a call that passes that cursor. `plan` SHALL write nothing outside the run object (targets are previewed, not created). The preview a caller sees SHALL be exactly what a subsequent `apply` commits, or `apply` SHALL refuse.

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

#### Scenario: Plan reads inline tags outside code

- **WHEN** a planned note carries a front-matter tag, an inline `#tag` in its body, and a hash inside an inline code span and inside a fenced code block
- **THEN** the item's tags are the front-matter tag followed by the inline tag
- **AND** neither hash inside code is a tag

#### Scenario: Plan recognizes an earlier import

- **WHEN** a collection that an earlier run imported is staged again under a new location with the same `collection` label and one note changed
- **THEN** every unchanged item records the `existing_ref` of its earlier Source
- **AND** the changed note plans as a new item that names its earlier Source as `previous_import`

#### Scenario: Apply refuses a stale plan

- **WHEN** `apply` is called with a `plan_id` that does not equal the stored plan's id, or the current selection hash no longer matches the plan's selection hash
- **THEN** it is refused with `PLAN_STALE` and writes nothing

### Requirement: First-question handoff on finish

`adoption_studio(action="finish")` SHALL be allowed from `applied` and `partial`, run a deterministic recall check (keyword `find` over the largest imported text title, or the largest artifact title when no text item exists, counting a hit only on the exact target path) producing `recall_check = {query, ok, hits}`, optionally write a run manifest under `Knowledge Base/_Adoption/`, and set phase `done`. When the recall check finds the target page, `finish` SHALL return a `first_question` referencing that title with a ready-to-run search route. When the recall check is pending or failed, or the search answered without the target page, `finish` SHALL NOT return a `first_question` or its route; it SHALL return `indexing` with the recall check's state (`pending`, `failed` or `missed`) and the recall sample's sampled, found, missed, pending and failed counts. A recall check that the search answered without the target page SHALL name a reconcile route. Every route that `finish` returns SHALL name a tool and arguments that the serving surface lists. `finish` SHALL also return `coverage`: outcome counts by status and code; skipped hidden folders and junk counted by reason with file counts; for artifact items, extraction states counted as done, pending, engine disabled, failed, or unknown; resolved and unresolved link counts; and a recall sample of up to ten applied items spread over the collection's top-level folders in a fixed order, counting a hit only on the exact target path, reported as found over sampled. A pending or unknown extraction SHALL never be reported as done. Open proposals SHALL NOT block finish; there is no engine timer or background thread. `status`/`finish` SHALL include a `handoff` block carrying `prompt_text` and deterministic `links`.

#### Scenario: Finish proves recall and offers the first question

- **WHEN** `finish` runs on a run with applied imports
- **THEN** `recall_check.ok` is true because an imported target appears in the keyword hits, `first_question` references the imported title with a search route that the serving surface lists, and the phase becomes `done`
- **AND** any run manifest is written only under `Knowledge Base/_Adoption/`

#### Scenario: Finish reports coverage honestly

- **WHEN** `finish` runs on a run whose applied items include an image waiting for extraction and a note with one unresolved link
- **THEN** `coverage` counts the image as pending and the link as unresolved
- **AND** the recall sample reports how many sampled items it found out of how many it sampled

#### Scenario: Finish withholds the first question while the pages are not searchable

- **WHEN** `finish` runs while the search index has not caught up with the imported pages
- **THEN** it returns no `first_question` and no route
- **AND** it returns `indexing` with the state `pending` and the recall sample's counts, and the phase becomes `done`

### Requirement: Own notes adopt as governed draft notes

A `select` folder rule SHALL accept `adopt_as` with the values `source` (the default) and `note`. A `note` rule SHALL name a `note_type` that the note-type registry holds as a compiled type, and MAY name the `project` or `domain` that the type's destination needs. `select` SHALL refuse an unregistered note type, or a type the note leaf cannot place, with a coded error and no selection change. The rule SHALL be part of the selection identity. The server SHALL NOT infer whether a file is an own note from its words or name.

For an own-note item, `plan` SHALL preview the Source copy exactly as for a `source` item and SHALL also preview a note target, title and front matter. The note body SHALL be the original note body after its front matter, unchanged, followed by the generated `## Links` section that a Source carries; its title and aliases SHALL come from the parsed front matter, its title is the front matter's `title` when it has one, and its tags SHALL be the item's planned tags: the front-matter tags followed by the body's inline `#tags` outside code. The note's `sources:` SHALL cite the item's Source copy, and the note SHALL carry the item's `import_collection` and `collection_path`. A link from an own note to another own-note item SHALL resolve to that item's planned note; any other resolved link SHALL resolve as it does for Sources. The note target SHALL be part of the planned actions hashed into `plan_id`.

`apply` SHALL commit each chunk's Sources as for `source` items, then create each own-note item's note with status `draft` through the governed two-phase note creation, recording the note's outcome in the run object with its commit. The server SHALL validate each note and bind it to its own draft token one at a time, and SHALL commit the notes of a chunk that pass in canonical batches with one Recent-activity bullet and one log entry per note. A call SHALL create one note at a time until it has made and timed a note; after that, a batch SHALL hold as many of the chunk's notes as fit in the time that the call has left, priced at the dearest time per made note that the call has seen. A refused note SHALL NOT lower that price. A batch that refuses before it writes SHALL fall back to creating each note of the chunk alone, so that each item receives the outcome that it receives alone. A batch that is written SHALL NOT fall back: each of its notes SHALL be created, and a note whose own step after the write failed SHALL report that error, as it does when the note is created alone. An own-note item SHALL be complete only when both its Source and its note exist. An item whose Source exists and whose note is still owed SHALL report the outcome `source-applied`, and progress SHALL count it as remaining, not as applied. The identity check SHALL find a note that an earlier run, a retry or a crashed apply created for the same collection path and record it as `already-applied` instead of creating another. An occupied note target SHALL fail the item with `TARGET_OCCUPIED`. For an item whose original changed since an earlier import, `plan` SHALL record the earlier note as `previous_note`, and `apply` SHALL create only the new Source.

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

#### Scenario: A chunk of own notes commits as one batch

- **WHEN** `apply` creates the notes of a chunk whose Sources are committed, and the call has room for every note
- **THEN** the call times its first note alone, the other notes commit as one canonical batch, and each note has one Recent-activity bullet and one log entry
- **AND** a note whose target another writer took fails with `TARGET_OCCUPIED` and the other notes commit

#### Scenario: A quick refusal does not price the next batch

- **WHEN** a call's first note is refused at once and each made note costs a fixed time
- **THEN** the call makes its next note alone before it commits a batch
- **AND** no call runs past its budget less its delivery reserve

#### Scenario: A refused batch falls back to one commit per note

- **WHEN** a chunk's batch refuses before it writes
- **THEN** each note of the chunk is created alone and every item receives the outcome that it receives alone

#### Scenario: A failure after the batch is written

- **WHEN** a step after the batch's write fails for one note of the chunk
- **THEN** every other note is created and that note reports the error
- **AND** no note of the chunk is created a second time

#### Scenario: The write fails after its commit point

- **WHEN** the batch's write fails after its commit point
- **THEN** no note of the chunk is created a second time
- **AND** the identity check records each note of the batch as `already-applied`
