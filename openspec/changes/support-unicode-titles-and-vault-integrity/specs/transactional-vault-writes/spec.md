## ADDED Requirements

### Requirement: Multi-File Markdown Batches Roll Back

The batch write primitive SHALL provide all-or-nothing observable filesystem state for ordinary caught staging or replacement failures. If commit fails after one or more destinations changed, the system MUST restore every pre-existing destination and remove every destination newly created by the failed batch before returning an error.

#### Scenario: Second replacement fails

- **WHEN** a two-file batch replaces the first destination and fails while replacing the second
- **THEN** both destinations have their exact pre-batch bytes after rollback
- **AND** the operation reports failure rather than success

#### Scenario: Failed batch included a new file

- **WHEN** a failed batch had already created a destination that did not exist before the batch
- **THEN** rollback removes that new destination

### Requirement: Moves Roll Back Link Rewrites

`move_file` SHALL treat the file relocation and its inbound wikilink rewrites as one reversible operation. A failure rewriting links MUST restore the original path and original inbound-link contents, and a failed relocation MUST leave inbound links untouched.

#### Scenario: Link rewrite fails after rename

- **WHEN** the target file is renamed and the inbound-link batch then fails
- **THEN** the target is restored at its original path
- **AND** all inbound-link files retain their original content
- **AND** the destination path does not remain as a duplicate

#### Scenario: Successful move remains single-copy

- **WHEN** a move and all inbound-link rewrites succeed
- **THEN** only the destination path exists
- **AND** every selected inbound wikilink targets the destination
