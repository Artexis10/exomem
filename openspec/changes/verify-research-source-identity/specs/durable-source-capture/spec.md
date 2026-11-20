## ADDED Requirements

### Requirement: A metadata-only correction reuses the predecessor's original

`capture_source` SHALL accept a correction without `files`, `content` or `passages` when the predecessor has a stored original.
It SHALL read that original and refuse with `SOURCE_ORIGINAL_CHANGED` when its SHA-256 differs from the predecessor's recorded hash.
It SHALL store those bytes as the successor's original under the predecessor's original filename and content type.
The successor SHALL take its title from the call.
It SHALL take its URL, kind, domain, projects, tags, document form and reason for capture from the call when the call sets them, and from the predecessor when the call omits them.
It SHALL take its compiled-note backlinks from the predecessor.
The successor SHALL receive its own identity check.
The successor's original SHALL be queued for media processing when it is committed, as every binary Source capture SHALL be, so its page receives the document's text without waiting for a file-watcher event.
A correction SHALL NOT be answered with an earlier Source that holds the same bytes.
The predecessor page and its original SHALL remain byte-identical, and every existing correction guard SHALL apply.

#### Scenario: A wrong DOI is corrected without a re-upload

- **WHEN** an agent corrects a binary Source with the predecessor's current page hash, a reason and the document's own DOI, and sends no file
- **THEN** one successor and one receipt are written
- **AND** the successor's original has the predecessor's SHA-256 and the successor lists the predecessor's backlinks
- **AND** the predecessor page and original are unchanged

#### Scenario: A corrected Source is processed

- **WHEN** a metadata-only correction of a PDF Source commits
- **THEN** the successor's page records `processing_state: pending` and a media job exists for its original

#### Scenario: A correction that names only what was wrong keeps the rest

- **WHEN** an agent corrects a binary Source with no file, the same title, a new URL, new tags and the correction, and omits every other field
- **THEN** the successor has the new URL and tags
- **AND** the successor keeps the predecessor's kind, domain, projects, document form and reason for capture

#### Scenario: The stored original no longer matches

- **WHEN** the predecessor's original differs from its recorded SHA-256
- **THEN** the correction refuses with `SOURCE_ORIGINAL_CHANGED` and writes nothing

#### Scenario: A correction is undone

- **WHEN** an agent corrects the successor back to the predecessor's metadata
- **THEN** a second successor and receipt are written, and every earlier page and receipt remains

### Requirement: Every Source correction keeps the fields it omits

A correction that supplies a new file, new `content` or new `passages` SHALL take its URL, kind, domain, projects, tags, document form, reason for capture and custody mark from the call when the call sets them, and from the predecessor when the call omits them, as a metadata-only correction does.
It SHALL NOT require `source_kind` when the predecessor records one.
Its successor SHALL keep owner-only raw protection when the predecessor has it, whatever the call sets.
A field that any correction sets to an empty value SHALL be empty in the successor, so an empty `url` removes a url the document does not support.

#### Scenario: A text Source's title is corrected

- **WHEN** an agent corrects a text Source with a new title, its text and the correction, and omits every other field
- **THEN** the successor has the new title
- **AND** the successor keeps the predecessor's kind, URL, domain, projects, tags and custody mark

#### Scenario: A correction drops a url its document does not support

- **WHEN** an agent corrects a PDF Source whose url names a DOI the document does not show, with a new title, a new kind and an empty `url`, and sends no file
- **THEN** the successor has no url and its identity check claims no DOI
- **AND** the successor keeps the predecessor's tags

### Requirement: An agent reaches a correction from what it reads

`options.correction.supersedes` SHALL accept every name `read_page` accepts for the Source: a ref, a path, a short id or a title.
A correction refused for missing fields SHALL name every missing field in one refusal.
A refusal for a wrong `expected_hash` SHALL say that it is the Source page's `content_hash` from `read_page`, not the file's SHA-256.
A refusal for an unknown or withheld Source SHALL say what to pass, in the same words for both.
A Source the caller can read but not write SHALL be refused with `SOURCE_CORRECTION_NOT_PERMITTED`.
When a title names several pages, `supersedes` SHALL prefer the append-only one.

#### Scenario: A Source is corrected by the id that search showed

- **WHEN** an agent corrects a Source and names it in `supersedes` by its short id or its title
- **THEN** the correction is written as if the agent had named the Source's ref

### Requirement: An in-place edit of a Source points to its correction

An in-place edit of a captured Source, of its body or of a frontmatter field that no pointer route owns, SHALL be refused with the correction call for that Source.
A caller that cannot record a correction SHALL be told so instead, and a caller that may not read the Source SHALL get neither.

#### Scenario: An edit of a Source's title points to the correction

- **WHEN** an agent edits a captured Source's title in place
- **THEN** the edit is refused with `APPEND_ONLY`, and the refusal names `options.correction` with that Source's ref

### Requirement: Search marks a corrected Source

A search result for a Source that has a correction receipt SHALL carry `corrected_by` with the title and path of the current successor, when the caller may read that successor.
The lookup SHALL cost at most one receipt read per returned Source result.
A withheld, missing or invalid successor SHALL add nothing to the result.

#### Scenario: A search finds the mislabelled predecessor

- **WHEN** a search returns a corrected Source
- **THEN** the result names the successor in `corrected_by`
- **AND** the result still lists the predecessor, so it stays discoverable
