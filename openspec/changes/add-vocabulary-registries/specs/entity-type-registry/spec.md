## MODIFIED Requirements

### Requirement: Registry Writes Are Guarded And Preserve Observed Types
The system SHALL validate proposed registry content before writing, SHALL use optimistic content-hash guards and atomic vault writes, and SHALL refuse a proposal that drops an entity type observed in authored vault state. Deprecation SHALL be the supported removal path. Every save, through the generic `save` delta or the `save-entity-types` alias, SHALL commit the overlay, a snapshot of the bytes it replaced and a `log.md` entry naming the reason and both hashes as one batch.

#### Scenario: Existing registry requires its current hash
- **WHEN** a save targets an existing extension registry without its current content hash or with a stale hash
- **THEN** it fails with `REGISTRY_EXISTS` or `STALE_ENTITY_TYPE_REGISTRY` and writes nothing

#### Scenario: Observed type cannot be deleted
- **WHEN** a proposed registry omits an extension ID present in `observed_ids`
- **THEN** the save fails with `OBSERVED_ENTITY_TYPE_DELETION`
- **AND** the existing registry remains unchanged

#### Scenario: A save keeps what it replaced
- **WHEN** an agent saves a new entity type with a reason
- **THEN** `_Schema/history/entity-types/` holds a snapshot of the previous overlay, or of the empty overlay when none existed
- **AND** `log.md` records the operation, the reason and both hashes

## ADDED Requirements

### Requirement: The core entity types come from a shipped pack
The core entity types SHALL be read from `src/exomem/vocabulary/packs/core/entity-types.yaml`, not from Python constants. For a vault with no overlay, and for a vault with an unchanged legacy overlay, the effective entity types, their folders, labels, aliases, cue nouns, optional frontmatter, families and fingerprint SHALL equal the values the Python constants produced.

#### Scenario: An unchanged vault resolves as before
- **WHEN** a vault with a legacy `entity-types.yaml` loads after the upgrade
- **THEN** every type, alias and family resolves exactly as it did before
- **AND** the overlay file is not rewritten

### Requirement: An entity-type save can be restored
`schema_memory(subject="entity-types", operation="history")` SHALL list the kept versions, newest first, with the operation, the time and both hashes; the reason and the principal SHALL be served to the owner only. `restore` SHALL take a kept version, the current hash and a reason, write that version's exact bytes as a governed save with its own snapshot, and report the keys it removed. A restore SHALL NOT rewrite any page. A page whose `entity_type` the restore removed SHALL keep its bytes and SHALL surface as unregistered debt through the existing `entity_type_unregistered` finding.

#### Scenario: Reverting a promotion leaves its page as debt
- **WHEN** an agent promotes `venue`, writes an entity page of type `venue`, and the owner restores the version before the promotion
- **THEN** `venue` no longer resolves and the restore reports it as removed
- **AND** the page's bytes are unchanged and audit reports it as `entity_type_unregistered`

#### Scenario: A hand-edited overlay is read with findings and kept on the next save
- **WHEN** the owner hand-edits `entity-types.yaml` so one entry is invalid and another is valid
- **THEN** the valid entry loads, the invalid entry becomes a finding, and the cache serves the edited file without a restart
- **AND** the next governed save snapshots the hand-edited bytes, so a restore can return to them exactly
