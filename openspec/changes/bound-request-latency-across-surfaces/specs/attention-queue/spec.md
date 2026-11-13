## ADDED Requirements

### Requirement: Write due advice describes affected pages

A mutation's due-state advice SHALL count only affected pages and SHALL carry `scope: affected_paths`. Bootstrap and explicit review SHALL retain global counts. Stored terminals without scope SHALL retain their historical global meaning. Unknown scope SHALL omit the advisory without changing the mutation outcome.

Affected pages SHALL include dependency buckets that the mutation's existing delta updates. A batch SHALL carry the union of those pages.

The two scopes SHALL retain separate emission history. A local block MUST NOT suppress or re-enable an unchanged global block. First-surfaced records SHALL use the category and fingerprint actually delivered. Existing disclosure, triage, dispositions, and client conversation guidance SHALL remain in force.

#### Scenario: A write touches one page among unrelated due items

- **WHEN** that page has one eligible item and unrelated pages also have due items
- **THEN** the write counts only its affected pages and declares that scope
- **AND** bootstrap and explicit review still report their global view

#### Scenario: Global and local advice alternate

- **WHEN** a global block is delivered, followed by a local block, followed by the unchanged global block
- **THEN** the local delivery neither suppresses nor re-enables the unchanged global block

#### Scenario: A record updates a matched plan's due state

- **WHEN** a record's declared join updates one Planning page while unrelated Planning pages also have due items
- **THEN** the response includes the matched page's advice and excludes the unrelated pages
- **AND** a batch includes only the union of pages affected by its writes

#### Scenario: The projection changes before delivery recording

- **WHEN** a delivered row's fingerprint differs from a later projection
- **THEN** first-surfaced recording uses the delivered fingerprint

### Requirement: A review item id binds from stored state

A decision that names a review item by its id SHALL bind the id before writer admission. For the owner, the stored due-state entry whose reference carries the id's head SHALL decide the id. An entry with the same fingerprint SHALL bind the decision. An entry with another fingerprint SHALL refuse `STALE_ITEM` with the entry's current id. A vault that has no stored projection SHALL build it once before it binds an id.

A listed item can fuse a stored entry with other signals of its page. When the first-surfaced ledger records that a review listing gave out this very id, the review queues SHALL decide it.

The service SHALL read a queue that it rebuilds from the vault only for an id whose head the first-surfaced ledger records from a review listing. It SHALL refuse any other unknown id `ITEM_NOT_FOUND` without a pass over the vault.

A restricted audience is served fingerprints recomposed over what it may see. Its ids SHALL bind only from the queues that this audience can read. A stored entry SHALL NOT answer a restricted audience.

#### Scenario: The keep-whole call of a note-cohesion question is decided

- **WHEN** a compiled write returns a note-cohesion question, and the agent sends the dismissal that the response spells out
- **THEN** the service saves the decision, and the next write within the same step asks nothing
- **AND** the decision reads no page other than the item's own

#### Scenario: A vault with no stored projection decides the same call

- **WHEN** the vault has no stored projection, and the agent sends the dismissal that a write response spells out
- **THEN** the service builds the projection once and saves the decision

#### Scenario: A stale id of a stored entry names the current id

- **WHEN** a page reaches its next step after its question was asked, and a decision names the earlier id
- **THEN** the service refuses `STALE_ITEM` with the current id
- **AND** it reads no page of the vault

#### Scenario: An id that the service never gave out is refused at once

- **WHEN** a decision names an id that no stored entry holds and that no review listing gave out
- **THEN** the service refuses `ITEM_NOT_FOUND`
- **AND** it reads no page of the vault

#### Scenario: A restricted caller names the id of a withheld page

- **WHEN** a restricted caller names an id of a stored entry whose page is withheld from it
- **THEN** the service refuses `ITEM_NOT_FOUND`, as it does for an id that does not exist
