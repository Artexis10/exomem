## ADDED Requirements

### Requirement: The import receipt lists each staged member

For an `adopt` import, the import Job SHALL record in its vault-state receipt a member manifest that lists each staged file's stage-relative path, size and SHA-256, computed while it unpacks. Adoption SHALL treat only files in that manifest as consumable staged copies. A discard SHALL remove the manifest with the stage.

#### Scenario: The receipt lists exactly what was staged

- **WHEN** an `adopt` import finishes
- **THEN** its receipt lists every file under `_Imports/<import_id>/` with the size and SHA-256 that the file has on disk, and nothing else

#### Scenario: Discard leaves nothing consumable

- **WHEN** an import is discarded
- **THEN** neither its stage nor its member manifest remains
