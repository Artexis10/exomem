## ADDED Requirements

### Requirement: The cell serves its release notes

`bootstrap(profile="compact", section="whats_new")` SHALL return `whats_new` with the running `version` and `releases` from the image's notes file, newest first. It SHALL read no vault content and SHALL answer from memory after the first read.

#### Scenario: Home reads the notes

- **WHEN** Home calls the `whats_new` section on a cell that runs 1.3.0
- **THEN** the result carries version 1.3.0 and the notes of 1.3.0 and older releases

#### Scenario: Image without notes

- **WHEN** the image carries no notes file
- **THEN** the section returns the running version and an empty `releases` list

### Requirement: A cloud cell tells each connected app once about an update

On a cloud cell, the cell SHALL keep, for each canonical gateway app id, the last version that app was told about. An app's first call SHALL only record the running version. When notes arrived since the recorded version, the next JSON-object tool result SHALL carry `update_notice`, after the cell records the running version. Without new notes, the cell SHALL only record it.

#### Scenario: First tool call after an upgrade

- **WHEN** an app told about 1.2.0 calls a tool on a cell that now runs 1.3.0, and 1.3.0 has a note
- **THEN** the result carries `update_notice` with version 1.3.0 and the note in `say`
- **AND** the app's next result carries no `update_notice`

#### Scenario: A new app

- **WHEN** an app with no record calls a tool
- **THEN** the result carries no `update_notice` and the running version is recorded for it

#### Scenario: Upgrade without notes

- **WHEN** the releases since the app's version have no notes
- **THEN** no result carries `update_notice` and the app's version moves to the running one

#### Scenario: The state store refuses the write

- **WHEN** recording the app's version fails
- **THEN** the result carries no `update_notice` and the call does not fail

### Requirement: The update notice is one bounded sentence for the assistant

`update_notice` SHALL carry `version` and `say`. `say` SHALL begin "Tell the user once in this conversation: ", give the newest note, count the other notes and point to What's new on Home, within 400 characters. It MUST carry no vault content.

#### Scenario: Three notes since the app's version

- **WHEN** three notes arrived since the app's version
- **THEN** `say` gives the newest note, says there are two more changes and points to What's new on Home
