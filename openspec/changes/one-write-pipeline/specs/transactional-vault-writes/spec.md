## ADDED Requirements

### Requirement: Validation runs the commit path to its preparation stop

Every write surface that writes owner pages SHALL validate by running its write through the commit path with native preparation, stopping before any effect. The plan SHALL be complete at the stop, including log, navigation index, trash metadata and curation witness writes. Validation SHALL bind the bytes of the pages the caller writes. The commit SHALL plan the log and navigation index writes again and check them again.

#### Scenario: An auxiliary write the caller may not make is refused at validation

- **WHEN** a caller who may write a note but not its log entry validates a create
- **THEN** validation refuses with the same code that commit would return
- **AND** nothing is written

#### Scenario: A concurrent write does not stale a validated commit through the log

- **WHEN** a caller validates a note edit, and another write appends to the log before the caller commits
- **THEN** the commit succeeds with the validated page bytes
- **AND** the log holds both entries

#### Scenario: Every planned path is checked at both steps

- **WHEN** the parity test denies, in turn, each path that a write command's commit plans
- **THEN** validation and commit return the same refusal code for each denied path

### Requirement: Names are resolved once, before the write is planned

Every write surface SHALL resolve vocabulary terms and folder names through one names function before it plans its write and hashes its draft. The batch write SHALL NOT rewrite a path or a byte. It SHALL refuse a planned new folder that the names function would still steer, given the path that a planned move vacates, and the refusal SHALL name the registered folder.

#### Scenario: Every surface steers a folder case variant the same way

- **WHEN** a tool write, an import run, a device upload and an episode save each name the folder `Notes/Research food`, and the vault holds `Notes/Research Food`
- **THEN** each write lands in `Notes/Research Food`
- **AND** each commit writes the bytes that its validation bound

#### Scenario: A write that skipped the names function is refused

- **WHEN** a surface plans a new folder `Notes/Research food` without calling the names function, and the vault holds `Notes/Research Food`
- **THEN** the batch write refuses it and names `Notes/Research Food`
- **AND** nothing is written

#### Scenario: A case-only rename is not refused

- **WHEN** a move renames `Notes/Research food` to `Notes/Research Food`
- **THEN** the batch write passes the vacated path to the check
- **AND** the move commits

#### Scenario: An import run item that needs a vocabulary decision stays staged

- **WHEN** an import run files a page whose domain has nearby registered meanings
- **THEN** that item stays in the import run's staging area with `VOCABULARY_DECISION_REQUIRED` and its candidates
- **AND** the other items of the batch are filed
