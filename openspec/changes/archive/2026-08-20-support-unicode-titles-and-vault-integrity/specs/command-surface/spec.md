## ADDED Requirements

### Requirement: Filename Slug Is Consistent Across Generated Surfaces

Every public create command whose shared leaf accepts an optional filename `slug` SHALL expose that same optional input through MCP, REST/OpenAPI, and CLI without duplicating slug validation or title behavior in a surface adapter.

#### Scenario: Product command uses explicit slug through REST

- **WHEN** a REST caller creates a titled page with an explicit valid slug
- **THEN** the shared leaf creates the same path and stored Unicode title as an equivalent MCP or CLI call

#### Scenario: Surface validation returns the shared error

- **WHEN** any generated surface receives an invalid explicit slug
- **THEN** it returns the shared leaf's stable validation error and no page is written
