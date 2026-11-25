## ADDED Requirements

### Requirement: Stored Identifiers Move to the Product Name Without Data Loss

Note identity, references, the import package, machine-local state paths and the Cloud infrastructure names that hold no state SHALL move to the `evermatter` name. Every reader SHALL accept both names before any writer emits the new one. Stored data SHALL move only on an explicit request, and each move SHALL be reversible without losing an edit. Protocol and derivation constants SHALL keep their bytes.

#### Scenario: A vault from before the rename keeps working

- **WHEN** a vault whose notes carry `exomem_id` is opened after the writers emit `evermatter_id`
- **THEN** every note keeps its identity and every stored reference resolves
- **AND** a note written afterwards carries `evermatter_id`

#### Scenario: The owner rewrites a vault and rolls it back

- **WHEN** the owner previews and then starts the rewrite job on a vault
- **THEN** the preview lists the notes that will change and the notes whose text names `exomem_id`
- **AND** the job skips any note edited since the plan, can resume after an interruption, and writes a receipt
- **AND** a rollback renames the key back, and it skips and lists any note edited since the rewrite

#### Scenario: A note holds two different identities

- **WHEN** a note holds `evermatter_id` and `exomem_id` with different values
- **THEN** the reader returns an integrity error and resolves neither value
- **AND** the audit names the note and says to keep one value

#### Scenario: An old reference resolves

- **WHEN** an agent reads a reference written as `exomem://memory/<uuid>`
- **THEN** the reference resolves to the same note as `evermatter://memory/<uuid>`

#### Scenario: Stored secrets and derived ids survive the package rename

- **WHEN** the import package is renamed
- **THEN** stored session records still decrypt, cell tokens still verify, derived ids are unchanged and old backups still verify

## MODIFIED Requirements

### Requirement: Exomem Is the Canonical Internal Name

The Python import package SHALL be `evermatter`, reachable as `python -m evermatter`. An `exomem` alias package SHALL keep the old imports and entry points working through the compatibility window. Settings SHALL be documented under the `EVERMATTER_` prefix.

#### Scenario: Package imports under the new name

- **WHEN** `import evermatter` (or any submodule) is executed in an installed environment
- **THEN** the package loads and all functionality is available under the `evermatter` name

#### Scenario: Code installed against the old package keeps running

- **WHEN** a hook, script or installed service unit imports `exomem` or runs `python -m exomem` during the compatibility window
- **THEN** it gets the same module objects and behaviour, and the deprecation line goes to the log, not to stdout

#### Scenario: Configuration documented under the new prefix

- **WHEN** a user consults the README or deployment docs for configuration
- **THEN** every variable is documented as `EVERMATTER_*`, with a note that `EXOMEM_*` and `KB_MCP_*` names remain honored
