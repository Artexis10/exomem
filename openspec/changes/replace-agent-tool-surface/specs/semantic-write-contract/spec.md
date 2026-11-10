## ADDED Requirements

### Requirement: Guards Ride In A Short Id That The Leaf Compares

A write that guards a version SHALL take one short id in place of hash, fingerprint and token fields. A short id SHALL be 7 base32 characters of the identity, a dot, and 4 base32 characters of the version. A unit id, a row id, a plan item id, a review item id and a page id SHALL follow this rule. The leaf SHALL resolve the identity and compare the version after the write is admitted and under the writer lease, never at the command seam. A stale id SHALL refuse with the current id and a retry that carries it. A restricted caller SHALL receive, for an id that names a withheld unit or row, the refusal that an unknown id receives.

#### Scenario: A stale unit id refuses with the current id
- **WHEN** a caller edits an observation by a unit id issued before the observation changed
- **THEN** the edit refuses and the refusal names the current unit id
- **AND** a retry with the current id succeeds

#### Scenario: A row id guards a record update
- **WHEN** a caller updates a record by the row id that the query returned
- **THEN** the update commits without any hash field

#### Scenario: An id of a withheld row reads as unknown
- **WHEN** a restricted caller names a row id that only a withheld row holds
- **THEN** the refusal equals the refusal for an id that no row holds

### Requirement: The Container Hash Is Filled At Commit

A records or plans write that names a row id SHALL NOT require `expected_container_hash`. The leaf SHALL fill it under the writer lease. REST and CLI SHALL keep accepting it.

#### Scenario: A row update needs no container hash
- **WHEN** a caller updates a row by id and sends no container hash
- **THEN** the update commits against the container version at commit time

### Requirement: An Unlinked Reason Replaces The Relation Review Triple

A note write over MCP SHALL accept one `unlinked_reason`. The server SHALL bind the reviewed-none decision and its reason to the exact committed content. A write that carries no reason and links nothing SHALL refuse with the existing relation-disposition refusal and a retry that names `unlinked_reason`.

#### Scenario: One call writes an unlinked note
- **WHEN** a caller creates a note that links no page and gives an unlinked reason
- **THEN** the note commits in that call with the reviewed-none decision recorded

### Requirement: A Row Query Returns The Id That A Row Write Takes

A row that query_records or query_plan_items returns SHALL carry its short id as `id`. Where the collection declares a field named `id`, the row SHALL keep that field and carry the short id as `row_id`. A stored field SHALL never be overwritten: a row that declares both `id` and `row_id` carries no short id. edit_record and edit_plan_item SHALL accept the short id under either name. A query that names its columns SHALL return the named columns and still carry the id on every row.

#### Scenario: A collection with its own id field keeps it
- **WHEN** a collection declares an `id` column and a client queries its rows
- **THEN** each row keeps its own `id` value and carries the short id as `row_id`
- **AND** edit_record accepts that `row_id` as `record`

#### Scenario: A collection that declares both names keeps both
- **WHEN** a collection declares an `id` column and a `row_id` column
- **THEN** each row keeps both stored values and carries no short id

### Requirement: A Review Item And An Advisory Carry The Id That Decides Them

Each item that list_review_items returns SHALL carry a short `id`. Each advisory that a write returns for a reviewable signal SHALL name its item by a short `id` or, where the advisory has none, by the write-advisory ref in its `review` field. decide_review_item SHALL accept the id or the ref as `review_item`. For a queue item it SHALL compare the id's version at the leaf, as for any other guarded id. For an advisory it SHALL decide the item at the fingerprint that the advisory last surfaced with. An upkeep item that a recall packet or session start carries SHALL carry the same `id`, and its `dispose` route SHALL name decide_review_item with that id on the public surface.

#### Scenario: An advisory id decides its item
- **WHEN** a write returns an advisory with an id
- **THEN** decide_review_item with that id as `review_item` dismisses the item

#### Scenario: An advisory ref decides its item
- **WHEN** a write returns an advisory that carries a write-advisory ref in `review` and no `id`
- **THEN** decide_review_item with that ref as `review_item` dismisses the item

#### Scenario: An upkeep item names a decision the surface lists
- **WHEN** a recall packet carries an upkeep item on the public surface
- **THEN** its `dispose` route names decide_review_item with the item's id as `review_item`
