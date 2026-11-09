## ADDED Requirements

### Requirement: Every Page Parameter Takes A Title Or A Short Id

Each MCP parameter that names a page SHALL accept the page's title or its short id beside a vault-relative path, a KB-relative path and an `exomem://` ref. These parameters are the page path of `read_memory`, `edit_note`, `change_observations`, `prepare_note`, `supersede_note` (`old_path`), `read_connections` (path and target), `change_connections` (accept-relation), `read_review` and `triage_memory`; `search_memory.relation_of`; `recall_context.anchor` and `conversation.refs`; and `approve_procedure.ref`. A parameter whose subject is a file SHALL keep taking paths only. The resolver SHALL try, in order, an `exomem://` ref, an existing path, a short id, then a title; a value that names nothing SHALL pass on unchanged so that each not-found and create-missing path behaves as before. A title SHALL resolve through the writer's shared resolver, so a page created by the previous call resolves. A short id SHALL be 7 base32 characters of the page's `exomem_id`, a dot, and 4 base32 characters of its content hash; a page without an `exomem_id` SHALL take a hash of its path in the first part. No published tool schema or description SHALL change.

#### Scenario: A page created a second ago resolves by its title
- **WHEN** a caller creates a page and reads it by its title in the next call
- **THEN** the read returns that page

#### Scenario: A path that exists keeps its meaning
- **WHEN** one page's path equals another page's title
- **THEN** the path names the first page and the title names the second

#### Scenario: An id names the page across edits
- **WHEN** a caller names a page by an id issued before the page was edited
- **THEN** the call selects that page, and an operation without a version guard applies

### Requirement: An Ambiguous Title Refuses With Candidates And A Retry

A title that several pages the caller may see share SHALL refuse with `AMBIGUOUS_REFERENCE` and the message `N pages are titled “X”.`. The refusal SHALL carry at most 5 candidates of `title`, `type`, `date` and `id`, newest first, and a retry that sets the parameter to `<id>`. A tie between pages SHALL first narrow to the pages the operation can use: an edit SHALL prefer pages that are neither append-only nor navigation pages. The visibility filter SHALL run before any page is counted or read: a withheld page SHALL NOT make a title ambiguous or appear as a candidate, and a title that only withheld pages hold SHALL refuse byte for byte as an unknown title does. For `recall_context.anchor`, anything other than exactly one eligible page SHALL give the existing anchor refusal.

#### Scenario: Two visible pages share a title
- **WHEN** a caller names a title that two pages hold
- **THEN** the refusal says "2 pages are titled “<title>”." and lists both pages with their ids
- **AND** a retry with one candidate's id resolves that page

#### Scenario: A withheld page of the same title changes nothing
- **WHEN** a restricted caller names a title that one visible page and one withheld page hold
- **THEN** the visible page resolves with no ambiguity, as in a vault without the withheld page

#### Scenario: A title only withheld pages hold reads as unknown
- **WHEN** a restricted caller names a title that only withheld pages hold
- **THEN** the refusal equals the refusal for a title no page holds

### Requirement: A Short Id Guards An Edit

`expected_hash` SHALL accept a short id or the 64-character hash. A short id SHALL guard the write against the version it names; when the page has moved past it, the write SHALL refuse with `STALE_EDIT`, the current id and a retry that carries it. An operation that requires an `expected_hash` in the leaf and received none for a page named by id SHALL take the version from that id. An optional guard SHALL stay optional.

#### Scenario: A stale id refuses with the current id
- **WHEN** a caller edits with an `expected_hash` id that names an older version
- **THEN** the write is refused with `STALE_EDIT`, nothing is saved, and the refusal's retry carries the current id
- **AND** one retry with that id succeeds
