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

### Requirement: The id of a stored due entry binds from the entry

A decision that names a review item by its id SHALL bind the id before writer admission. For the owner, when a stored due-state entry has exactly this id, the entry SHALL bind the decision, and the service SHALL NOT read a review queue to bind it. Any other id SHALL bind from the review queues.

A restricted audience is served fingerprints recomposed over what it may see. Its ids SHALL bind only from the queues that this audience can read. A stored entry SHALL NOT answer a restricted audience.

#### Scenario: The keep-whole call of a note-cohesion question is decided

- **WHEN** a compiled write returns a note-cohesion question on a vault that holds its due-state projection, and the agent sends the dismissal that the response spells out
- **THEN** the service saves the decision, and the next write within the same step asks nothing
- **AND** the decision reads no page other than the item's own

#### Scenario: An id that no stored entry has binds from the review queues

- **WHEN** a decision names an id, and no stored entry has exactly this id
- **THEN** the review queues decide the id, as they did before this requirement

#### Scenario: A restricted caller names the id of a withheld page

- **WHEN** a restricted caller names the id of a stored entry whose page is withheld from it
- **THEN** the service gives the same refusal as for an id that does not exist
