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

### Requirement: Governed server-side relation acceptance

`connect_memory(operation="accept-relation")` SHALL accept exactly one selected
queue item through the existing governed edit path, with its ref, fingerprint,
source hint, current content hash, and non-empty audit reason. The operation
SHALL append one canonical relation bullet under `## Relations` only after
revalidation. Hash or fingerprint drift MUST refuse without writing. A committed
response SHALL preserve explicit pending graph synchronization rather than
claiming the derived edge is already current.

When the item offers specific relations the caller SHALL name one as
`requested_relation`. A relation among the options the server computes for that
item at accept time SHALL need no vocabulary binding; any other selected
relation SHALL need the exact binding of a recorded vocabulary decision. A call
that names such a relation without the binding SHALL refuse with
`VOCABULARY_APPLICATION_INVALID`, name the offered relations and write nothing.
A supplied binding SHALL be validated exactly as before. The caller MAY take the
ref, fingerprint and source content hash from the queue item or from the
`review` of a `suggest_relations` candidate.

#### Scenario: Accept writes one canonical bullet
- **WHEN** the selected candidate's source hint, ref, fingerprint, and page hash
  are current and the caller supplies an audit reason
- **THEN** exactly one canonical relation bullet is committed by the governed
  writer with its normal receipt and recovery semantics

#### Scenario: Drift refuses the write
- **WHEN** the source hash or candidate fingerprint no longer matches
- **THEN** the operation writes nothing and returns refresh-required guidance

#### Scenario: Accepted item leaves the queue
- **WHEN** an accepted relation is present in the newly current graph snapshot
- **THEN** the queue excludes that already-authored edge

#### Scenario: An offered relation is accepted in one call
- **WHEN** a caller takes `review.ref`, `review.fingerprint` and
  `review.source_content_hash` from a `suggest_relations` candidate that offers
  specific relations, and accepts it with `requested_relation` set to one of
  the offered options, a non-empty reason and no vocabulary binding
- **THEN** the governed writer commits exactly one canonical bullet of that
  relation under `## Relations`
- **AND** the queue no longer lists the item

#### Scenario: A relation the item does not offer is refused
- **WHEN** a caller accepts with `requested_relation` set to an active relation
  that is not among the options the server computes at accept time, and
  supplies no vocabulary binding
- **THEN** the call refuses with `VOCABULARY_APPLICATION_INVALID` before any
  write, and names the offered relations
- **AND** the remediation names the vocabulary decision that would admit the
  relation, or dismissing the candidate
