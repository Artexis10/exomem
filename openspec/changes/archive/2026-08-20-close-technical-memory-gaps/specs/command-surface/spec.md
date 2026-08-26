## ADDED Requirements

### Requirement: Technical gap commands preserve registry parity
The product registry SHALL expose `schema_memory`, stable-reference parameters and response fields, `connect_memory(operation="context")`, and `maintain_memory(mode="backfill-ids")` consistently across MCP, REST, CLI, OpenAPI, generated capability docs, and schema-fidelity tests.

#### Scenario: One registry exposes every new route
- **WHEN** generated surfaces are inspected
- **THEN** the new command and modes are present with identical parameter semantics and no hand-maintained duplicate implementation

### Requirement: Paths and references coexist
Commands that accept governed page identifiers SHALL resolve paths and canonical references through one shared resolver and SHALL return both `path` and `ref` where they identify a durable governed artifact.

#### Scenario: Surface responses carry durable identity
- **WHEN** a source, note, entity, or evidence sidecar is created through MCP, REST, or CLI
- **THEN** each surface reports the same vault-relative path and canonical reference
