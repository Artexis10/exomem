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

#### Scenario: A text Source's title is corrected

- **WHEN** an agent corrects a text Source with a new title, its text and the correction, and omits every other field
- **THEN** the successor has the new title
- **AND** the successor keeps the predecessor's kind, URL, domain, projects, tags and custody mark

### Requirement: Search marks a corrected Source

A search result for a Source that has a correction receipt SHALL carry `corrected_by` with the title and path of the current successor, when the caller may read that successor.
The lookup SHALL cost at most one receipt read per returned Source result.
A withheld, missing or invalid successor SHALL add nothing to the result.

#### Scenario: A search finds the mislabelled predecessor

- **WHEN** a search returns a corrected Source
- **THEN** the result names the successor in `corrected_by`
- **AND** the result still lists the predecessor, so it stays discoverable
