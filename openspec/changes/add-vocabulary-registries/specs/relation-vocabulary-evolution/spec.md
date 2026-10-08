## ADDED Requirements

### Requirement: Core relations ship as a vocabulary pack
The portable core relations SHALL be read from `src/exomem/vocabulary/packs/core/relations.yaml`. Moving the file SHALL NOT change any core key, description, family, direction, inverse or origin set, and SHALL NOT change the registry hash an unchanged vault reports.

#### Scenario: The moved pack resolves identically
- **WHEN** an unchanged vault loads its relation registry after the upgrade
- **THEN** the core keys, families and extension hash equal their previous values

### Requirement: Relation saves keep history and can be restored
Every relation-registry save, through the generic `save` delta or the `save-relations` alias, SHALL commit the overlay, a snapshot of the replaced bytes and a `log.md` entry as one batch, and the canonical batch writer SHALL still inject the graph epoch for the registry target. `history` and `restore` SHALL behave as for entity types. A restore SHALL write the kept bytes exactly, SHALL be exempt from the in-place meaning-continuity check because reverting is its purpose, and SHALL leave every Markdown edge unchanged; edges whose key the restore removed SHALL resolve as unregistered until a key is registered again.

#### Scenario: A relation promotion is reverted
- **WHEN** the owner restores the relation registry version before `vault.applies_to` was saved
- **THEN** `vault.applies_to` no longer resolves and the graph epoch advances
- **AND** Markdown that uses `applies_to` is byte-identical and its edges report `unregistered`
