## ADDED Requirements

### Requirement: Every release publishes its own user notes file

Each published release SHALL attach `release-notes.json`, built from the release tag in the user notes file format (`schema` 2), with the writer and the cell's reader sharing one format definition. It SHALL hold only that release's notes: at most one entry, with the version, `released_on` (the tag's date) and the notes the generator gives that release. A release with no notes SHALL attach an empty `releases` list, so a missing file always means the notes are missing.

#### Scenario: Release with notes

- **WHEN** release 1.3.0 contains two new note files
- **THEN** its `release-notes.json` has `schema` 2 and one entry for 1.3.0 with those two notes, newest first, and no other release

#### Scenario: Release without notes

- **WHEN** release 1.3.1 contains no new note file
- **THEN** its `release-notes.json` is `{"schema": 2, "releases": []}`, and the cell's notes reader accepts it

#### Scenario: Notes come from the release's own tag

- **WHEN** a run builds the file of release 1.3.0 after release 1.4.0 exists and a note of 1.3.0 was edited in 1.4.0
- **THEN** the file holds the note as 1.3.0's tag has it

### Requirement: The notes file format is a published contract

The format of `release-notes.json` SHALL be a contract with readers outside this repository. Any change to the format SHALL raise `schema`, and a reader SHALL accept the new schema before a release emits it. The format module SHALL name the outside reader. A consumer SHALL place a release in a month by its GitHub publish time, not by `released_on`.

#### Scenario: The format changes

- **WHEN** a change alters a key, the version shape or the date shape of the notes file
- **THEN** it raises `schema`, and no release emits the new schema until the monthly digest accepts it

#### Scenario: A consumer groups releases by month

- **WHEN** the monthly digest decides which month a release belongs to
- **THEN** it uses the release's GitHub publish time and not `released_on`

### Requirement: Publishing the notes never gates the release

The notes file, the page section and the upload SHALL run in their own job, which needs the plan and `build-artifacts` and which no publish, image or deploy job needs. A failure in that job SHALL fail that job only. The release plan SHALL NOT count the notes file when it decides whether a release is complete. A dry run SHALL build the notes of its own tag and SHALL write nothing to GitHub.

#### Scenario: The notes cannot be built

- **WHEN** the notes of a release cannot be built
- **THEN** the notes job fails and the PyPI, image and cell rollout jobs run as they would have

#### Scenario: A published file has other bytes

- **WHEN** release 1.3.0 already has a `release-notes.json` and a run builds the file with different bytes
- **THEN** the upload refuses to replace it and the notes job fails
- **AND** no other release artifact is changed or held

#### Scenario: Dry run

- **WHEN** a dry run builds the artifacts of a release
- **THEN** the notes job builds that release's notes and neither uploads the file nor edits the release page

### Requirement: Every run publishes the notes that the newest releases lack

Every run of the release workflow SHALL check the 20 newest published releases and publish the file and the page section for each one that has assets and lacks the file, building each from its own tag. A draft is not published. A release with no asset SHALL be left alone, because the release controller reads its page to write the changelog before publication starts. The upload SHALL use the existing release upload path, which refuses to replace a published file with other bytes.

#### Scenario: A failed run left a release without its notes

- **WHEN** the run that published release 1.3.0 failed before it uploaded the notes file
- **THEN** the next run, on any push or dispatch, builds the notes from the 1.3.0 tag and publishes the file and the section

#### Scenario: A release from before this change

- **WHEN** release 1.2.0 is among the 20 newest and has no notes file
- **THEN** the next run builds its notes from the 1.2.0 tag and publishes them

#### Scenario: Every release has its file

- **WHEN** the 20 newest published releases all have the file
- **THEN** the run builds nothing, uploads nothing and edits no page

#### Scenario: Publication has not started

- **WHEN** a release exists with no asset because the release controller has not finished with it
- **THEN** the run leaves that release's page and files alone

### Requirement: A release page starts with What's new

The release page body SHALL start with a "What's new" section above the technical changelog, listing each note's title, then its sentence. A note without a title SHALL show its sentence alone, and a release with no notes SHALL get no section. A job SHALL write the section whenever its hidden marker is absent from the page of a release it processes, and SHALL leave a page that has the marker as it is. It SHALL write the section before it uploads the file.

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

- **WHEN** a run processes a release whose page already has the marker
- **THEN** it leaves the body as it is and the page holds one section

#### Scenario: Marker absent

- **WHEN** a person deleted the section and a later run processes that release because its file is missing
- **THEN** the run writes the section again, once

### Requirement: Note text shows on the page letter for letter

The section SHALL show each title and sentence exactly as written. A note's text SHALL NOT produce an @-mention, an issue or pull request reference, an autolink or emphasis on the public release page. The page text SHALL be escaped for GitHub Markdown; the notes file SHALL keep the original text.

#### Scenario: Hostile text

- **WHEN** a note says `Ask @someone about #123 at https://example.com and use * freely`
- **THEN** GitHub's Markdown renderer shows that text with no mention, no issue link, no link and no emphasis
