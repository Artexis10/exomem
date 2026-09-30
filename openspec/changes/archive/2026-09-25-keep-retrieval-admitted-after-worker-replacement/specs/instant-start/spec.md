## ADDED Requirements

### Requirement: Retrieval stays admitted across a worker replacement

A serving process that inherits a maintained lexical catalogue published by an earlier process SHALL be able to bless that catalogue with an ordinary delta on its first governed write.

When warm-up proves an inherited catalogue checkpoint state-equal to this process's live projection, the serving process SHALL re-stamp that checkpoint in its own registry lineage. It SHALL do so under the catalogue publication barrier and without changing any row. A standby SHALL NOT re-stamp; it only proves.

A bounded catalogue mutation can apply its rows and still be unable to bless a live scope, because the scope has no stored checkpoint or no retained history bridges the stored checkpoint to the live one. In a managed runtime, such a mutation SHALL hand that scope to the catalogue repair owner, so readiness converges without a process restart. The readiness probe SHALL remain side-effect free.

#### Scenario: First write after a replacement keeps retrieval admitted

- **WHEN** a managed process starts on a catalogue an earlier process published, and warm-up admits retrieval
- **AND** the process then serves one governed write
- **THEN** the write blesses both recall scopes at this process's live checkpoint
- **AND** no catalogue repair runs
- **AND** the readiness proof keeps retrieval admitted

#### Scenario: A stranded scope converges through repair

- **WHEN** a governed write in a managed runtime applies its catalogue rows but cannot bless a live scope, because no retained history bridges that scope's stored checkpoint
- **THEN** the write hands the scope to the catalogue repair owner
- **AND** once that repair publishes a current catalogue, retrieval is admitted again without a process restart
