## ADDED Requirements

### Requirement: The write that creates a candidate delivers it
When a committed durable write adds a body wikilink to an identity that resolves to no
vault page and no active Entity, and that write brings the identity's count of distinct
eligible linking pages to the wikilink lane's spread gate, the committed response SHALL
carry an `entity_candidate` block of at most three identities, each with its name, at
most eight linking pages, registry near matches, and the routes to `resolve-entity` and
`create-entity`. The count SHALL be derived from vault state through the graph's
link-dependency index, without a vault walk and without any per-caller record. A write
by a page that already linked the identity, or for an identity already at or past the
gate, SHALL carry no block. Pages committed within one mutation batch SHALL count once.
The block SHALL be withheld when the `structural_suggestions` authority class is `off`
and when derived sync for the write is deferred. The server SHALL never create an Entity.

#### Scenario: The agent that holds the context is told
- **WHEN** one eligible page already links an unresolved identity and a second page that
  links it is written
- **THEN** that write's response carries the identity with both pages and the two routes

#### Scenario: A third page does not repeat the prompt
- **WHEN** two eligible pages already link an unresolved identity and a third is written
- **THEN** the response carries no block for that identity

#### Scenario: Editing a page that already linked it does not fire
- **WHEN** a page that already links an unresolved identity is edited and still links it
- **THEN** the response carries no block for that identity

#### Scenario: The same answer for every caller
- **WHEN** the crossing write arrives over stateless HTTP with no stable session
- **THEN** the block is delivered, because nothing about it depends on who is calling

#### Scenario: One command writing two notes counts once
- **WHEN** a single multi-write command commits two notes that link the same unresolved
  identity and no earlier page links it
- **THEN** neither response carries a block for that identity

#### Scenario: An owner who turned suggestions off is not prompted
- **WHEN** the `structural_suggestions` class is `off`
- **THEN** no write response carries the block, and the candidate remains in the
  `entity_recurrence` family

#### Scenario: A link to an existing Entity is an edge, not a candidate
- **WHEN** a write links a name that resolves to an active Entity
- **THEN** the graph holds the link's edge after the write and no block is carried

### Requirement: The write surface teaches linking
The `body` argument descriptions of `remember` and `replace_memory` SHALL tell the agent,
in one sentence, to wikilink the identities the note names whether or not a page exists
yet. The shipped capture hook SHALL permit creating an Entity for an identity that is
stable and either central to the note or recurring, and SHALL NOT require recurrence.

#### Scenario: A client with only the tool schema is told
- **WHEN** a client reads the `remember` tool's schema and nothing else
- **THEN** the `body` description tells it to link the identities the note names

#### Scenario: The hook allows a first-mention Entity
- **WHEN** a session wrote one note about an identity that has no Entity
- **THEN** the hook's text permits resolving and creating it without a second mention
