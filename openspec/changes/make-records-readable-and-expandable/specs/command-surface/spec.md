## ADDED Requirements

### Requirement: The generic Records command exposes exact child expansion and presentation refresh

The existing `record_memory` command SHALL keep one finite product surface and SHALL add `expand_child` only to query plus `refresh_presentation` only to update. Query SHALL accept either an explicit child-field string or the backward-compatible boolean selector under their declared compatibility rules. Update SHALL accept `refresh_presentation: true` with normal changes or as the sole semantic request, but SHALL refuse false/no-op refresh, refresh on a collection without a valid presentation recipe, and all use outside update. MCP, CLI, REST, action allowlists, saved views, bootstrap guidance, schema fixtures, and generated contracts SHALL expose the same argument names and behavior.

#### Scenario: Explicit child selector is discoverable everywhere
- **WHEN** a client inspects the public Records schema or calls query over MCP, CLI, or REST
- **THEN** `expand_child` has the same bounded string contract and reaches the same governed query leaf on every surface

#### Scenario: Presentation repair does not add another tool
- **WHEN** a caller needs to backfill a readable body for an existing item
- **THEN** it uses guarded `record_memory(action="update", refresh_presentation=true, ...)` and no separate renderer, migration, or YAML tool is added

#### Scenario: Selector leakage is refused
- **WHEN** `expand_child` is supplied to a non-query action or `refresh_presentation` is supplied to a non-update action
- **THEN** the command rejects the request as invalid arguments before opening collection or item contents
