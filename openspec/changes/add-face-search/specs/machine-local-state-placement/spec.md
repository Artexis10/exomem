## MODIFIED Requirements

### Requirement: Each state family declares whether it is rebuildable

Every reserved state family SHALL declare, as closed attributes of its registry entry, whether the product can rebuild it and whether it stays out of every backup. No other module SHALL keep a list or pattern of file names for either purpose, except a mirror in another package that a test pins to the registry. An external family SHALL set the rebuild attribute, or the registry SHALL refuse to load. An `external-canonical` family SHALL be not rebuildable and SHALL NOT stay out of backups.

#### Scenario: A canonical family

- **WHEN** a family is registered as `external-canonical`
- **THEN** it declares itself not rebuildable and does not stay out of backups
- **AND** a registry that registers one that does not fails to load

#### Scenario: A new family

- **WHEN** a new external family is registered without the rebuild attribute
- **THEN** the registry fails to load, so the family never joins or leaves a backup by default

#### Scenario: A family that stays out of backups

- **WHEN** a family declares itself not rebuildable and staying out of every backup
- **THEN** no backup stages it
- **AND** the Cloud cell backup's exclusion mirror lists its files, or its pin test fails
