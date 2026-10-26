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

### Requirement: Registry findings never block pages beyond their severity

Page validation SHALL report each relation-registry finding with that finding's own severity.
An unknown field in a saved vault overlay SHALL be a warning, so that a field written by a newer release never blocks page writes after a rollback.

#### Scenario: A newer overlay field survives a rollback
- **WHEN** a saved relation overlay carries a field that this release does not know
- **THEN** page validation reports a warning and page writes still commit

### Requirement: A saved alias keeps its meaning when a core key arrives

When a newer core pack adds a key equal to an alias that a saved vault overlay already binds to an extension, the loader SHALL keep that alias bound to the extension and SHALL report a `core_shadowed` warning.
The core meaning SHALL stay reachable through the explicit spelling `core.<key>`.
Grandfathering SHALL apply to aliases that the saved overlay held before the core key existed, not to every alias of an older extension.
A new save SHALL still refuse an alias that collides with a core key.

#### Scenario: Bare text keeps its meaning across a core upgrade
- **WHEN** a vault saved `vault.has_pet` with the alias `has_pet`, and a later core pack adds `has_pet`
- **THEN** existing pages that write `has_pet` keep resolving to `vault.has_pet`, page writes stay available, and `core.has_pet` names the core relation

### Requirement: Relation labels resolve in the admitted instance and are written bare

Every relation-label resolver SHALL select the caller's or the page's admitted registry instance, including `resolve-relation`, queue selection and acceptance, the bootstrap vocabulary section and curation identity.
Queue acceptance SHALL write the bare label when it resolves to exactly one entry in that instance, and the namespaced key otherwise.

#### Scenario: Queue acceptance writes the authored label
- **WHEN** the owner accepts a queued relation as `has_pet` in a vault whose only `has_pet` is the alias of `vault.has_pet`
- **THEN** the page gains `- has_pet [[...]]` and the graph edge resolves to `vault.has_pet`

#### Scenario: A private label stays private
- **WHEN** a caller who cannot admit a private instance resolves a label that only that instance defines
- **THEN** the result is the same as for an unregistered label

