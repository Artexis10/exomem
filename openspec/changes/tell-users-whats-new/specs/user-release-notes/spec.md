## ADDED Requirements

### Requirement: A user-visible change carries one plain note

A pull request whose change a user would notice SHALL add one file under `user-notes/` that states the change for a person who does not code. A note SHALL be one paragraph of at most 200 characters with no Markdown syntax, and SHALL name no module, tool, file path or internal term.

#### Scenario: A fix users will notice

- **WHEN** a pull request makes photos keep their real capture date
- **THEN** it adds a note such as "Photos now keep the date they were taken, even when your chat app removed it."

### Requirement: Every feat, fix or perf pull request decides about a note

The pull request check SHALL fail a pull request whose title type is `feat`, `fix` or `perf` unless it adds at least one `user-notes/*.md` file or its body has a line that reads `User note: none`. The check SHALL fail a note that breaks the shape rule. It MUST NOT judge a note's wording.

#### Scenario: Internal fix without a note

- **WHEN** a `fix` pull request adds no note and its body says `User note: none`
- **THEN** the check passes

#### Scenario: Feature with no decision

- **WHEN** a `feat` pull request adds no note and its body has no `User note: none` line
- **THEN** the check fails and says to add a note or the line

### Requirement: Notes belong to the release that first contains them

The release build SHALL assign each note to the earliest version tag whose history contains the commit that added the note's file, and a note in no tag to the version being built. It SHALL write the notes as JSON with `schema` 1 and `releases`, each with `version`, `released_on` and `notes`, newest first, keeping the 20 newest releases that have notes. No release step SHALL edit or delete a note file.

#### Scenario: Two releases with notes

- **WHEN** release 1.2.0 contains one new note and release 1.3.0 contains two
- **THEN** the file lists 1.3.0 with two notes, then 1.2.0 with one

#### Scenario: A note that skipped the check

- **WHEN** a note file breaks the shape rule at release time
- **THEN** the build leaves that note out, warns with the file name and does not fail

#### Scenario: Release without notes

- **WHEN** a release contains no new note file
- **THEN** the file lists no entry for that release

### Requirement: The cell image carries its notes

Each cell image SHALL carry the notes file generated for its own release as package data. A build without the file SHALL serve no notes, marked as unavailable, and never fail.

#### Scenario: Image for 1.3.0

- **WHEN** the release workflow builds the cell image for 1.3.0
- **THEN** the image holds the notes of 1.3.0 and older releases and none of a later one
