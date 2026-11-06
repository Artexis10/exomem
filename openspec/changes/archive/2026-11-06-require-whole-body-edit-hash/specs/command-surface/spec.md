## MODIFIED Requirements

### Requirement: Edit Memory Advertises One Discriminated Operation

The public `edit_memory` command SHALL remain one tool and SHALL advertise a required nested operation discriminated by `kind`. Supported variants MUST preserve current behavior for `replace_body`, `replace_tags`, `replace_string`, `batch_replace`, `edit_section`, `patch_frontmatter`, and `fill_row`, except that `replace_body` SHALL require `expected_hash`. Each variant SHALL forbid unrelated fields and expose only guards the underlying operation enforces.

#### Scenario: Agent selects replace string
- **WHEN** the client inspects the `replace_string` variant
- **THEN** it sees the exact old/new string, replace-all, supported tag composition, drift guard, and preview fields
- **AND** it does not see frontmatter, row, section, batch, or whole-body fields

#### Scenario: Invalid variant fields are submitted
- **WHEN** a caller adds a field belonging to another variant or omits a required variant field
- **THEN** validation fails before mutation with the selected kind and precise field guidance

#### Scenario: A whole-body edit without a hash is refused
- **WHEN** a caller sends `edit_memory` on MCP, REST or CLI, nested or in the flat compatibility form, with a whole-body edit and no `expected_hash` or a null one
- **THEN** the call fails with `INVALID_EDIT` before mutation, saying to send the `content_hash` of a whole-page read
- **AND** the page is unchanged, including for a `validate_only` preview

#### Scenario: The legacy edit alias refuses the same way
- **WHEN** a caller sends the opt-in legacy `edit` alias or a direct runtime call with `new_body` and no `expected_hash`
- **THEN** the call fails with `INVALID_EDIT` naming `expected_hash`
- **AND** the same call with the page's current `content_hash` succeeds
