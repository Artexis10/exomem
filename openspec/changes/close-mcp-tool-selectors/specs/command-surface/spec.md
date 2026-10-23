## ADDED Requirements

### Requirement: Mixed command selectors publish their closed value set

Every MCP product command that the selector registry (`governance.egress.selector_registry()`) covers SHALL publish its selector parameter with a JSON Schema `enum` that lists exactly the registry's values for that command and selector. The registry SHALL stay the one source of those values: a selector whose leaf signature already declares a `Literal` SHALL publish the same set, and a value the registry does not cover SHALL NOT be accepted on MCP.

#### Scenario: A prose-only selector gains an enum

- **WHEN** a client lists the tools of a server built from the registry
- **THEN** `connect_memory.operation`, `schema_memory.operation`, `manage_memory_file.operation`, `review_memory.mode` and `adoption_studio.action` each carry an `enum`
- **AND** each `enum` equals the selector registry's values for that command

#### Scenario: An unknown selector value is refused before the leaf

- **WHEN** an MCP client calls `connect_memory` with `operation="drop-everything"`
- **THEN** the call fails input validation
- **AND** no leaf runs and no writer lease is taken
