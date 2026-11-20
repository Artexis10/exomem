# Spec Delta

## ADDED Requirements

### Requirement: Each state family declares whether it is rebuildable

Every reserved state family SHALL declare whether the product can rebuild it from the vault or by recomputation. The declaration SHALL be one closed attribute on the family's registry entry, and no other module SHALL keep a list or a pattern of file names for the same purpose. An external family SHALL choose, and the registry SHALL refuse to load one that leaves the attribute unset. A family that is `external-canonical` SHALL declare itself not rebuildable. A backup SHALL stage exactly the external families that declare themselves not rebuildable.

#### Scenario: A canonical family

- **WHEN** a family is registered as `external-canonical`
- **THEN** it declares itself not rebuildable
- **AND** a registry that registers one that does not fails to load

#### Scenario: A new family

- **WHEN** a new external family is registered without the attribute
- **THEN** the registry fails to load, so the family never joins or leaves a backup by default
