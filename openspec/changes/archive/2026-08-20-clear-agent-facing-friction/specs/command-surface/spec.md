## ADDED Requirements

### Requirement: Edit Memory Advertises One Discriminated Operation

The public `edit_memory` command SHALL remain one tool and SHALL advertise a required nested operation discriminated by `kind`. Supported variants MUST preserve current behavior for `replace_body`, `replace_tags`, `replace_string`, `batch_replace`, `edit_section`, `patch_frontmatter`, and `fill_row`. Each variant SHALL forbid unrelated fields and expose only guards the underlying operation enforces.

#### Scenario: Agent selects replace string
- **WHEN** the client inspects the `replace_string` variant
- **THEN** it sees the exact old/new string, replace-all, supported tag composition, drift guard, and preview fields
- **AND** it does not see frontmatter, row, section, batch, or whole-body fields

#### Scenario: Invalid variant fields are submitted
- **WHEN** a caller adds a field belonging to another variant or omits a required variant field
- **THEN** validation fails before mutation with the selected kind and precise field guidance

### Requirement: Legacy Edit Calls Normalize Before Idempotency

For one compatibility release, the runtime SHALL accept the previous flat `edit_memory` arguments even though they are not part of the primary advertised schema. Exactly one legacy mode MUST be present. Both legacy and discriminated forms SHALL normalize to the same canonical operation before payload hashing, lease acquisition, and leaf invocation.

#### Scenario: Old and new clients retry the same edit
- **WHEN** equivalent flat and discriminated edit payloads use the same idempotency identity
- **THEN** they resolve one canonical payload digest and one leaf execution
- **AND** the retry returns the committed terminal rather than `IDEMPOTENCY_KEY_REUSED`

#### Scenario: Legacy modes are mixed
- **WHEN** a flat call supplies fields for multiple exclusive edit modes or combines flat fields with `operation`
- **THEN** it fails before mutation with an `INVALID_EDIT`-class error naming the conflict

### Requirement: Edit Schema Is Consistent With Runtime Acceptance

The MCP discovery schema and REST OpenAPI schema SHALL publish the discriminated primary shape, and black-box calls through those adapters SHALL prove each published variant reaches the existing edit leaf. The compatibility shim MUST be tested separately and marked deprecated with a one-release minimum.

#### Scenario: MCP schema and call agree
- **WHEN** the live FastMCP tool list is inspected and each operation variant is invoked against an isolated vault
- **THEN** the schema contains the discriminator and forbids unrelated fields
- **AND** every valid published call performs or previews exactly its selected edit mode
