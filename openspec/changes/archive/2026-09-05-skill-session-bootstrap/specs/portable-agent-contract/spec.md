## MODIFIED Requirements

### Requirement: MCP-Only Distribution Is Sufficient

A generic client with only the MCP server's tool list and portable `compact`, `full`, or `diagnostics` bootstrap operation SHALL receive enough information to create a compliant compiled note. Those generic profiles SHALL include the complete minimum semantic-authoring object, and authoring-tool descriptions SHALL include the exact compact syntax, `## Observations` location, open-category rule, and required-minimum/remediation that applies to that tool. A valid `session` profile is the attested exception: it MAY omit generic semantic-authoring, Records, Planning, and front-door teaching only after the matching installed skill contract has been accepted, while retaining the live due-state/family-disposition post-write guidance required to interpret returned state.

#### Scenario: Session profile relies on the attested installed skill
- **WHEN** a client presents the matching installed skill contract with `profile="session"`
- **THEN** the response omits generic semantic-authoring teaching
- **AND** portable profiles remain complete for clients without that installed skill

#### Scenario: Generic MCP client can author without a skill
- **WHEN** a client receives tool schemas and calls default `bootstrap()` without an installed skill or repository instructions
- **THEN** it can distinguish category, tag, and kind; choose compact or rich form; satisfy the minimum; and remediate a refusal through the preferred write route

#### Scenario: Tier-2 schema warns at the escape-hatch moment
- **WHEN** a client inspects `manage_memory_file` create, overwrite, or append guidance
- **THEN** it learns that compiled destinations receive the same semantic contract and is directed to `remember` or `replace_memory` when those typed routes fit
