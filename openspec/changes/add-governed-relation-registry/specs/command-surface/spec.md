## ADDED Requirements

### Requirement: Relation governance preserves registry-generated surface parity
The product registry SHALL expose relation registry and traversal-profile
infer/validate/diff/save behavior through the schema-governance command, and
traversal profile selection through the context command, with identical
parameters and error semantics across MCP, REST, CLI, OpenAPI, generated docs,
annotations, and schema-fidelity fixtures. Existing schema/context calls SHALL
remain compatible when the new subject/profile parameters are omitted.

#### Scenario: One registry definition exposes relation governance everywhere
- **WHEN** the generated surfaces are inspected after this change
- **THEN** schema governance accepts relation/profile subjects and reviewed
  proposals, context accepts traversal profiles, and every surface exposes the
  same defaults, bounds, hash guards, and validation codes

#### Scenario: Existing callers retain prior behavior
- **WHEN** callers omit relation-governance subjects and traversal profiles
- **THEN** existing schema contract behavior and broad context traversal remain
  unchanged
