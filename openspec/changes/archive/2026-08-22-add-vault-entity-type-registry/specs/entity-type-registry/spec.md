## ADDED Requirements

### Requirement: Vaults Extend The Core Entity Registry
The system SHALL load `_Schema/entity-types.yaml` schema version 1 as a vault-owned extension mapping layered beside the unchanged core entity types. Invalid entries SHALL become deterministic findings while valid sibling entries continue loading. Active resolution SHALL include valid active extensions and SHALL exclude deprecated extensions.

#### Scenario: Valid extension loads beside core
- **WHEN** a vault defines an active synthetic `place` entity type
- **THEN** the active registry contains `place` and all five core IDs
- **AND** resolving the extension ID, folder, label, or alias returns `place`

#### Scenario: Invalid entries soft-fail independently
- **WHEN** extension entries collide with core or extension IDs, aliases, labels, or folders, use unsafe folder segments, or name a non-core parent
- **THEN** each invalid entry produces a finding rather than an exception
- **AND** every valid remainder entry still loads

#### Scenario: Cue nouns default to aliases
- **WHEN** an extension omits `cue_nouns`
- **THEN** its cue nouns are the extension aliases

### Requirement: Registry Writes Are Guarded And Preserve Observed Types
The system SHALL validate proposed registry content before writing, SHALL use optimistic content-hash guards and atomic vault writes, and SHALL refuse a proposal that drops an entity type observed in authored vault state. Deprecation SHALL be the supported removal path.

#### Scenario: Existing registry requires its current hash
- **WHEN** a save targets an existing extension registry without its current content hash or with a stale hash
- **THEN** it fails with `REGISTRY_EXISTS` or `STALE_ENTITY_TYPE_REGISTRY` and writes nothing

#### Scenario: Observed type cannot be deleted
- **WHEN** a proposed registry omits an extension ID present in `observed_ids`
- **THEN** the save fails with `OBSERVED_ENTITY_TYPE_DELETION`
- **AND** the existing registry remains unchanged

### Requirement: Extension Folders Are Materialized Only When Needed
Initialization SHALL create folders for valid active extension types only when the extension file already exists. Creating an entity of a valid extension type SHALL lazily create its folder when absent. Indexing and entity enumeration SHALL include every active extension folder and invalidate cached structure when the registry identity changes.

#### Scenario: First extension entity creates its folder lazily
- **WHEN** an active extension type exists and its entity folder does not
- **THEN** creating an entity of that type creates the folder and entity page atomically through the existing entity writer path

#### Scenario: Registry change rebuilds entity indexes
- **WHEN** the extension content hash changes
- **THEN** cached folder matching and per-type indexes are rebuilt from the new active registry

### Requirement: Unregistered Entity Types Surface As State-Resolved Findings
Audit SHALL emit deterministic `entity_type_unregistered` findings when an entity page declares a type absent from the active registry or at least three pages live beneath an unregistered immediate entity folder. Each folder finding SHALL carry a ready proposal containing normalized ID, folder, title-cased label, empty aliases, and page count. The finding SHALL resolve only when the registry or pages change and SHALL NOT be automatically written or dismissible into silence.

#### Scenario: Declared unknown type surfaces
- **WHEN** an entity page declares an entity type absent from the active registry
- **THEN** attention surfaces an `entity_type_unregistered` finding with a ready proposed entry

#### Scenario: Folder threshold is three pages
- **WHEN** an unregistered entity folder contains three pages
- **THEN** audit emits the folder finding
- **AND** the same folder with two pages emits no folder-threshold finding

#### Scenario: Registration resolves the finding
- **WHEN** the proposed type is registered or the triggering pages move to registered state
- **THEN** the next audit and attention pass no longer contains the finding

### Requirement: Registry Loading Remains Bounded
Loading a fifty-type extension registry over the eight-thousand-page latency fixture SHALL add less than 50 milliseconds of attributable cold cost (registry parse, cue-noun build, and entity-folder enumeration, each from a cleared cache), SHALL keep cold whole-find wall time within a quarter of the no-registry baseline or 50 milliseconds, whichever is larger, and SHALL add zero measured milliseconds to the warm cached path without loosening any existing threshold.

#### Scenario: Cold and warm latency gate
- **WHEN** the entity type registry latency test runs against the scale fixture
- **THEN** the attributable cold overhead is below 50 milliseconds
- **AND** cold whole-find wall time stays within the backstop ratio of the no-registry baseline
- **AND** the warm cached overhead is measured as zero milliseconds
