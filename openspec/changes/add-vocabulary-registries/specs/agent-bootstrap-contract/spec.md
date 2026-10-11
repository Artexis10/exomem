## ADDED Requirements

### Requirement: Bootstrap serves the live vocabulary by use
The compact bootstrap SHALL serve a `vocabulary` section. For each registry it SHALL list at most 12 active keys ordered by usage count with their counts, a `+n more` line when more keys exist, the number of registry findings and the `schema_memory(operation="inspect")` route for that registry. A registry MAY declare that the section omits its keys. For such a registry the section SHALL still report a findings count that is not zero, its recently added keys under `new`, and `unavailable` when the registry refuses the caller; it SHALL leave the registry out only when none of these applies. Usage counts SHALL come from maintained projections. When a projection is absent, warming, stale or refused, that registry's counts SHALL read `unavailable` with a reason and its keys SHALL be listed in registry order; the section SHALL NOT report a zero it did not count. Usage counts SHALL follow `owner_only_aggregate`, including RAW protection without configured file policy. A caller that it refuses SHALL receive `unavailable` with `audience_restricted`. The always-served core SHALL carry a pointer to the section instead of entity-type ids. `section="entities"` SHALL remain accepted and SHALL serve the `vocabulary` section's blocks. Released hosted profiles SHALL keep their published payload.

#### Scenario: A promoted type appears with its count
- **WHEN** an agent promotes an entity type and the graph projection is current
- **THEN** `bootstrap(section="vocabulary")` lists the type under `entity-types` with a counted value
- **AND** after an entity page of that type is indexed, its count is 1

#### Scenario: A cold projection is unavailable, not zero
- **WHEN** the graph projection has not been built
- **THEN** the entity-type and relation counts read `unavailable` with a reason
- **AND** no key carries a count of 0

#### Scenario: The core points at the section
- **WHEN** a client reads the compact core
- **THEN** `capture_semantics` names the `vocabulary` section instead of listing entity types
- **AND** the core stays under its byte ceiling
