## ADDED Requirements

### Requirement: Every release publishes its own user notes file

Each published release SHALL attach `release-notes.json`, built from the release tag in the user notes file format (`schema` 2). It SHALL hold only that release's notes: at most one entry, with the version, `released_on` and notes that the notes generator gives that release. A release with no notes SHALL attach the file with an empty `releases` list, so a missing file always means the notes are missing. The writer and the cell's reader SHALL share one format definition.

#### Scenario: Release with notes

- **WHEN** release 1.3.0 contains two new note files
- **THEN** its `release-notes.json` has `schema` 2 and one entry for 1.3.0 with those two notes, newest first, and no other release

#### Scenario: Release without notes

- **WHEN** release 1.3.1 contains no new note file
- **THEN** its `release-notes.json` is `{"schema": 2, "releases": []}`, and the cell's notes reader accepts it

### Requirement: The notes file is a required release asset

The release job SHALL fail when it cannot build or upload `release-notes.json`. The file SHALL upload through the existing release upload path, which uploads an asset only when it is missing and refuses to replace a published asset with different bytes. The release plan SHALL count the file for a tag that carries its builder, so a later push or dispatch publishes a release that lacks it; an older tag stays complete without it. A dry run SHALL build the file and upload nothing.

#### Scenario: The upload fails

- **WHEN** the notes file cannot be built or uploaded
- **THEN** the release job fails and the next run on that tag publishes the file

#### Scenario: Rerun after a complete release

- **WHEN** a run on a release that already has `release-notes.json` builds the same bytes
- **THEN** it uploads nothing and the release page stays as it is

#### Scenario: Dry run

- **WHEN** a dry run builds the artifacts of a release
- **THEN** it builds `release-notes.json` and neither uploads it nor edits the release page

### Requirement: A release page starts with What's new

The release page body SHALL start with a "What's new" section above the technical changelog. The section SHALL list each note of the release: its title, then its sentence. A note without a title SHALL show its sentence alone. A release with no notes SHALL get no section. A marker that the page does not show SHALL make the write happen once, so a rerun or a recovery run adds no second copy.

#### Scenario: Release with titled notes

- **WHEN** a release has a note titled "Photos keep their date" with its sentence
- **THEN** the page starts with "What's new", the title and then the sentence, and the changelog follows

#### Scenario: Note without a title

- **WHEN** a release has a note with a null title
- **THEN** the section shows that note's sentence alone

#### Scenario: Release without notes

- **WHEN** a release has no notes
- **THEN** the page body stays the technical changelog alone

#### Scenario: Run again on the same release

- **WHEN** a run finds the page already starts with the section
- **THEN** it leaves the body as it is
