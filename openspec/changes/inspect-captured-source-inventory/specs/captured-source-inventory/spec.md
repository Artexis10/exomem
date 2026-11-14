## ADDED Requirements

### Requirement: Governed bounded canonical capture inventory

The system SHALL provide one read-only inventory operation through the shared MCP, CLI and REST registry.
Its public MCP operation SHALL be `inspect_memory` with `operation: "sources.inventory"`.
It SHALL require existing whole-namespace content authority and accept one identity system, an optional record, a keyset cursor and a limit from 1 to 200.
It SHALL return canonical Source identities and references in deterministic key order.
Canonical companion classification SHALL exclude bound originals even when they contain identity-shaped fields.
Classification SHALL cover all Markdown files in canonical raw lanes regardless of their semantic scan eligibility.
An owner or target change SHALL update all affected binding dependents without rescanning every Source.

#### Scenario: List revisions for one record

- **WHEN** an authorized caller supplies a system, record and page limit
- **THEN** the operation returns at most that many canonical captured revisions for that record
- **AND** a continuation cursor advances within the same scope

#### Scenario: A bound Markdown original claims an identity

- **WHEN** a bound original contains Source identity or passage declarations
- **THEN** the inventory excludes that original under the maintained companion classification

#### Scenario: A companion lies outside semantic scan membership

- **WHEN** a canonical raw-lane Markdown page or its companion is excluded from semantic scanning
- **THEN** inventory still classifies the page and binding from the canonical raw-lane inputs

#### Scenario: A companion owner is deleted

- **WHEN** deleting an owner changes an unchanged original's canonical classification
- **THEN** the same published inventory update reclassifies that dependent original

#### Scenario: A caller lacks namespace authority

- **WHEN** that caller requests inventory
- **THEN** the operation refuses before disclosing identities or Source references

#### Scenario: A cursor belongs to another scope

- **WHEN** a caller supplies a malformed cursor or a cursor for another system or record
- **THEN** the operation returns a field-specific error without widening the scope

### Requirement: Distinguish complete inventory from incomplete observation

Every inventory page SHALL identify its observed incarnation-qualified corpus generation and completeness.
A successful empty inventory SHALL require complete publication proof for the requested scope.
File-mode proof SHALL cover the live full freshness caption and exclude unpublished external edits.
Store-mode proof SHALL cover the pinned revision and canonical ownership of included pages.
Cold, failed or unavailable proof MUST remain explicit unknown or pending state rather than successful absence.
Traversal SHALL be live keyset traversal, with each page's generation visible to its consumer.

#### Scenario: An external edit has not reached the publisher

- **WHEN** inventory encounters unpublished external changes
- **THEN** it reports incomplete observation
- **AND** it does not claim that a missing identity is absent

#### Scenario: The corpus changes between pages

- **WHEN** a later page observes a different generation
- **THEN** that page reports the changed generation
- **AND** the consumer can repeat reconciliation instead of treating the traversal as one stable snapshot

#### Scenario: An integer generation repeats after restart

- **WHEN** a later page uses another publisher incarnation with the same integer generation
- **THEN** its observation token differs from the earlier page's token

### Requirement: Prove exact passage indexing in the active space

Each item SHALL report `indexed`, `not_yet_indexed`, `failed`, `unknown` or `not_applicable`.
The item SHALL identify its canonical capture revision and the observed recall space.
Counts SHALL be nullable and failures SHALL be bound to the corresponding capture revision.
The system SHALL report `indexed` only from exact chunk publication evidence that remains current for that Source and active recall space.
Row counts, queue absence and historical acknowledgements alone MUST NOT establish that state.

#### Scenario: Incorrect chunk text preserves the row count

- **WHEN** stored chunk rows have the expected count but lack current exact publication proof
- **THEN** inventory does not report indexed

#### Scenario: The active recall space changes

- **WHEN** a receipt describes an earlier recall space
- **THEN** it does not prove indexing in the current space

#### Scenario: An unrelated Source is indexed

- **WHEN** the maintained path log proves that this Source's exact publication remains unchanged
- **THEN** another Source's publication does not invalidate its receipt

#### Scenario: Volatile receipts are lost on restart

- **WHEN** inventory cannot prove an item's exact chunk publication
- **THEN** it reports unknown and permits bounded background reconstruction
- **AND** it does not reconstruct Source text or scan vectors on the request path
- **AND** proof-only recovery progresses even when no semantic mutation debt exists

#### Scenario: A page contains two hundred indexed Sources

- **WHEN** exact receipts are reconstructed for those active Sources
- **THEN** receipt retention can cover the page independently of the existing 64-entry semantic proof cache
- **AND** store-mode proof reads canonical stored bytes without encoding or projected-file substitution

### Requirement: Request work remains bounded as the corpus grows

Inventory SHALL seek a maintained identity bucket rather than scan all canonical Sources on each request.
Validation SHALL use a constant number of set-based database statements for one page, independent of its item count.
Requests MUST NOT read unselected Source bodies, allocate a full vector matrix or wait for proof reconstruction.
The declared inventory reference SHALL contain 20,000 captured Sources with a maximum page of 200 items and a p95 target below 200 ms.
That target SHALL remain unverified until the public-boundary measurement establishes it.

#### Scenario: Increase a page from two items to two hundred

- **WHEN** both requests use a current publication on the declared reference
- **THEN** database statement count remains constant
- **AND** the larger page reads no additional unselected Source bodies

