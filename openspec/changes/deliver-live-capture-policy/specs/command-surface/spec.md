## MODIFIED Requirements

### Requirement: Edit Memory Advertises One Discriminated Operation

The public `edit_memory` command SHALL remain one tool and SHALL advertise a required nested operation discriminated by `kind`. Supported variants MUST preserve current behavior for `replace_body`, `replace_tags`, `replace_string`, `batch_replace`, `edit_section`, `patch_frontmatter`, and `fill_row`, except that `replace_body` SHALL require `expected_hash` and that `edit_section` SHALL accept an optional `create_missing` boolean, false by default. When `create_missing` is true and the heading is absent, `edit_section` SHALL append a new level-2 section that holds `new_string`, under the same hash guard, and SHALL leave the rest of the body unchanged apart from its trailing newlines. A `heading` that is empty or spans more than one line SHALL refuse with `INVALID_EDIT` before mutation. Otherwise a missing heading SHALL refuse with `HEADING_NOT_FOUND` and a remediation that names the flag. Each variant SHALL forbid unrelated fields and expose only guards the underlying operation enforces.

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

#### Scenario: A missing section is created on request
- **WHEN** a caller sends `edit_memory` with an `edit_section` variant whose heading is absent from the page and `create_missing` set to true
- **THEN** the page gains one new level-2 section with that heading and the new text at the end of the body
- **AND** the rest of the body is unchanged apart from its trailing newlines, the `updated` stamp moves as for any edit, and the edit obeys the same `expected_hash` guard as any section edit

#### Scenario: A missing section refuses without the flag
- **WHEN** a caller sends an `edit_section` variant whose heading is absent and omits `create_missing`
- **THEN** the call fails with `HEADING_NOT_FOUND` before mutation
- **AND** the remediation names `create_missing` as the retry

#### Scenario: A section is not created under a heading that is not one line
- **WHEN** a caller sends an `edit_section` variant with `create_missing` set to true and a `heading` that is empty or spans more than one line
- **THEN** the call fails with `INVALID_EDIT` before mutation
- **AND** the page is unchanged
