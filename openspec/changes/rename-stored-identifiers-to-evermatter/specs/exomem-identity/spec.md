## ADDED Requirements

### Requirement: Stored Identifiers Move to the Product Name Without Data Loss

Note identity, references, state paths, the import package and the Cloud infrastructure names SHALL move to the `evermatter` name. Every reader SHALL accept both names before any writer emits the new one. Stored data SHALL move only on an explicit request, with a backup, and each move SHALL be reversible until it completes.

#### Scenario: A vault from before the rename keeps working

- **WHEN** a vault whose notes carry `exomem_id` is opened after the writers emit `evermatter_id`
- **THEN** every note keeps its identity and every stored reference resolves
- **AND** a note written afterwards carries `evermatter_id`

#### Scenario: The owner rewrites a vault

- **WHEN** the owner previews and then starts the rewrite job on a vault
- **THEN** the preview lists the notes that will change and the notes whose text names `exomem_id`
- **AND** the job copies each note before it changes it, can resume after an interruption, and writes a receipt
- **AND** a rollback from the receipt restores every note it changed

#### Scenario: A note holds two different identities

- **WHEN** a note holds `evermatter_id` and `exomem_id` with different values
- **THEN** the audit reports an integrity error for that note
- **AND** no reader picks one of the two values silently

#### Scenario: An old reference resolves

- **WHEN** an agent reads a reference written as `exomem://memory/<uuid>`
- **THEN** the reference resolves to the same note as `evermatter://memory/<uuid>`

#### Scenario: Code installed against the old package keeps running

- **WHEN** a hook or script imports `exomem` during the compatibility window
- **THEN** the import succeeds and logs one deprecation line
