## ADDED Requirements

### Requirement: Write due advice describes affected pages

A mutation's due-state advice SHALL count only affected pages and SHALL carry `scope: affected_paths`. Bootstrap and explicit review SHALL retain global counts. Stored terminals without scope SHALL retain their historical global meaning. Unknown scope SHALL omit the advisory without changing the mutation outcome.

The two scopes SHALL retain separate emission history. A local block MUST NOT suppress or re-enable an unchanged global block. First-surfaced records SHALL use the category and fingerprint actually delivered. Existing disclosure, triage, dispositions, and client conversation guidance SHALL remain in force.

#### Scenario: A write touches one page among unrelated due items

- **WHEN** that page has one eligible item and unrelated pages also have due items
- **THEN** the write counts only its affected pages and declares that scope
- **AND** bootstrap and explicit review still report their global view

#### Scenario: Global and local advice alternate

- **WHEN** a global block is delivered, followed by a local block, followed by the unchanged global block
- **THEN** the local delivery neither suppresses nor re-enables the unchanged global block

#### Scenario: The projection changes before delivery recording

- **WHEN** a delivered row's fingerprint differs from a later projection
- **THEN** first-surfaced recording uses the delivered fingerprint
