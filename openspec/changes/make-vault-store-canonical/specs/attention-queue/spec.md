## ADDED Requirements

### Requirement: The owner sees missing relationship endpoints in attention

In store mode, the default attention union SHALL include `relationship_orphan` after `relation_debt` and before `entity_type_unregistered`. Only the owner with unrestricted content access SHALL see its findings.

#### Scenario: A stored relationship loses an endpoint

- **WHEN** the unrestricted owner lists attention items without naming a category
- **AND** a stored relationship names a missing endpoint
- **THEN** attention includes the finding in its ranking as `relationship_orphan`
