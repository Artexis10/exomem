## MODIFIED Requirements

### Requirement: Bootstrap exposes collection guidance compactly

Bootstrap SHALL expose all finite Records actions and a bounded product-facing collection-authoring summary. Compact bootstrap SHALL identify `_collection.md`, supported Records collection versions and profiles, and the `describe -> validate -> create -> inspect -> append` agent workflow. It SHALL route clients to `record_memory(action="describe")` for the complete technical contract and SHALL NOT embed the full manifest JSON Schema, parser field table, or worked manifest. It SHALL teach intent before storage vocabulary and SHALL NOT imply that guidance activates a collection or migration.

#### Scenario: Compact bootstrap routes without exposing parser internals

- **WHEN** a generic client calls compact bootstrap
- **THEN** it can identify the exact route for agent-facing manifest discovery and read-only validation
- **AND** the payload does not contain the complete JSON Schema or complete manifest example

#### Scenario: Bootstrap guidance is not mutation

- **WHEN** bootstrap includes Records authoring guidance
- **THEN** no collection, folder, template, migration, or canonical data is activated merely by reading bootstrap
