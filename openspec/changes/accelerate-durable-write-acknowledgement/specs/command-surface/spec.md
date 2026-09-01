## ADDED Requirements

### Requirement: Deferred Write Advisory Results Have Exact-Only Retrieval

`review_memory` SHALL expose a read-only
`mode="write-advisory-result"` that requires one opaque
`exomem://write-advisory-result/<id>` reference and returns exactly that job's
current `pending`, `ready`, `failed`, or `superseded` state. It SHALL have no
list, browse, search, rank, count, continuation, or implicit-current form. A
malformed, unknown, unauthorized, or expired reference SHALL return the shared
indistinguishable not-found outcome. The generated command documentation and
surface fixtures SHALL describe the new mode and reference requirement without
adding a new product command.

#### Scenario: Exact result is pending and later ready

- **WHEN** a caller resolves the result reference returned by a committed write before and after its background advisory completes
- **THEN** the first lookup returns only `status="pending"`
- **AND** the later lookup returns `status="ready"` with the bounded currently authorized advisory warnings

#### Scenario: No reference is supplied

- **WHEN** `review_memory(mode="write-advisory-result")` is called without `ref`
- **THEN** it returns an explicit invalid-review error
- **AND** it does not list or summarize advisory jobs

#### Scenario: Existing product surface remains singular

- **WHEN** the command registry and MCP, REST, and CLI projections are regenerated
- **THEN** `review_memory` exposes the mode consistently through the existing shared leaf
- **AND** no new advisory-status command or facade-specific route is added
