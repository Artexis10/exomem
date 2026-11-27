## ADDED Requirements

### Requirement: Validation and commit share one plan function

Every write surface that writes owner pages SHALL validate through the same plan function that its commit runs. The plan function SHALL complete the plan, including log, navigation index, trash metadata and curation witness writes, and SHALL run the access check on every planned write. The commit SHALL run it again under its lock.

#### Scenario: An auxiliary write the caller may not make is refused at validation

- **WHEN** a caller who may write a note but not its log entry validates a create
- **THEN** validation refuses with the same code that commit would return
- **AND** nothing is written

#### Scenario: Every planned path is checked at both steps

- **WHEN** the parity test denies, in turn, each path that a write command's commit plans
- **THEN** validation and commit return the same refusal code for each denied path

### Requirement: Validation stays read-only and binds only the caller's pages

Validation SHALL NOT take the mutation boundary, the writer lease or the store writer. It SHALL bind the bytes of the pages the caller writes. The commit SHALL plan the other writes again and check them again.

#### Scenario: Validation overlaps a live writer

- **WHEN** a validate-only write overlaps another mutation
- **THEN** validation answers without returning `MUTATION_BUSY` because that writer holds the boundary
- **AND** the later commit still runs the plan function under its lock

#### Scenario: A concurrent write does not stale a validated commit through the log

- **WHEN** a caller validates a note edit, and another write appends to the log before the caller commits
- **THEN** the commit succeeds with the validated page bytes
- **AND** the log holds both entries

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
