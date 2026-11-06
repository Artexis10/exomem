## ADDED Requirements

### Requirement: A whole-body edit names the page it replaces

Every edit that replaces a page's whole body SHALL carry `expected_hash`, the `content_hash` of a whole-page read of that page. The `replace_body` variant of `edit_memory` SHALL advertise `expected_hash` as required. The nested operation, the flat compatibility form and the CLI and REST `edit` command SHALL refuse a whole-body edit without `expected_hash` before mutation, including a `validate_only` preview. The refusal SHALL be an `INVALID_EDIT`-class error that names `expected_hash` and says to read the whole page for it. Edit kinds that do not replace the whole body SHALL keep `expected_hash` optional.

#### Scenario: A whole-body edit without a hash is refused
- **WHEN** a caller sends `edit_memory` with `kind` `replace_body` and no `expected_hash`
- **THEN** the call fails with `INVALID_EDIT` naming `expected_hash`
- **AND** the page is unchanged

#### Scenario: The CLI path refuses the same way
- **WHEN** a caller sends the flat `edit` command with `new_body` and no `expected_hash`
- **THEN** the call fails with `INVALID_EDIT` naming `expected_hash`
- **AND** the same call with the page's current `content_hash` succeeds

#### Scenario: A windowed read cannot authorise a whole-body write
- **WHEN** a caller reads a long Source with `offset` and receives no `content_hash`
- **THEN** it has no hash to send, and a whole-body edit built from that window is refused
