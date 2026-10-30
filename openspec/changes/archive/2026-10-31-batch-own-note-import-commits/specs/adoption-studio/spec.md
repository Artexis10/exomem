## MODIFIED Requirements

### Requirement: Own notes adopt as governed draft notes

A `select` folder rule SHALL accept `adopt_as` with the values `source` (the default) and `note`. A `note` rule SHALL name a `note_type` that the note-type registry holds as a compiled type, and MAY name the `project` or `domain` that the type's destination needs. `select` SHALL refuse an unregistered note type, or a type the note leaf cannot place, with a coded error and no selection change. The rule SHALL be part of the selection identity. The server SHALL NOT infer whether a file is an own note from its words or name.

For an own-note item, `plan` SHALL preview the Source copy exactly as for a `source` item and SHALL also preview a note target, title and front matter. The note body SHALL be the original note body after its front matter, unchanged, followed by the generated `## Links` section that a Source carries; its title, tags and aliases SHALL come from the parsed front matter, and its title is the front matter's `title` when it has one. The note's `sources:` SHALL cite the item's Source copy, and the note SHALL carry the item's `import_collection` and `collection_path`. A link from an own note to another own-note item SHALL resolve to that item's planned note; any other resolved link SHALL resolve as it does for Sources. The note target SHALL be part of the planned actions hashed into `plan_id`.

`apply` SHALL commit each chunk's Sources as for `source` items, then create each own-note item's note with status `draft` through the governed two-phase note creation, recording the note's outcome in the run object with its commit. The server SHALL validate each note and bind it to its own draft token one at a time, and SHALL commit the notes of a chunk that pass in canonical batches with one Recent-activity bullet and one log entry per note. A call SHALL time its first note alone; after that, a batch SHALL hold as many of the chunk's notes as fit in the time that the call has left. A batch that refuses before it writes SHALL fall back to creating each note of the chunk alone, so that each item receives the outcome that it receives alone. A batch that is written SHALL NOT fall back: each of its notes SHALL be created, and a note whose own step after the write failed SHALL report that error, as it does when the note is created alone. An own-note item SHALL be complete only when both its Source and its note exist. An item whose Source exists and whose note is still owed SHALL report the outcome `source-applied`, and progress SHALL count it as remaining, not as applied. The identity check SHALL find a note that an earlier run, a retry or a crashed apply created for the same collection path and record it as `already-applied` instead of creating another. An occupied note target SHALL fail the item with `TARGET_OCCUPIED`. For an item whose original changed since an earlier import, `plan` SHALL record the earlier note as `previous_note`, and `apply` SHALL create only the new Source.

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
