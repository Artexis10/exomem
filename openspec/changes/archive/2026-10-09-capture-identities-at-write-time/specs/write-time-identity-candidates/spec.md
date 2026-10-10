## ADDED Requirements

### Requirement: The write that creates a candidate delivers it
When a committed durable write adds a body wikilink to a bare name that resolves to no
vault page and no active Entity, and that write takes the set of eligible pages linking
that same bare name from one page to two, the committed response SHALL carry an
`entity_candidate` block of at most three identities, each with its name, at most eight
linking pages, registry near matches, and the routes to `resolve-entity` and
`create-entity`, together with one fixed sentence of guidance telling the agent to
resolve before creating and to hydrate an existing Entity before making a second one. The set SHALL be read from the graph's link-dependency index, without a
vault walk and without any per-caller record, SHALL evaluate at most sixteen returned
rows, and SHALL apply the family's page-level exclusions: ineligible evidence, navigation
pages, pages in the `Entities` subtree, and a page whose own title or filename stem is
the name. A suffixed name that stands on a real file SHALL carry no block. The block is
advisory and MAY diverge from the `entity_recurrence` family in either direction; the
family remains authoritative. A write by a page that already linked
the name, or for a name already linked from two or more eligible pages, SHALL carry no
block. Pages committed within one mutation batch SHALL count once. The block SHALL be
withheld when the `structural_suggestions` authority class is `off` and whenever the
graph index does not answer the lookup as available. The server SHALL never create an
Entity.

#### Scenario: The agent that holds the context is told
- **WHEN** one eligible page already links an unresolved identity and a second page that
  links it is written
- **THEN** that write's response carries the identity with both pages and the two routes

#### Scenario: The block says what to do with it
- **WHEN** a client with no skill and no hook receives the block
- **THEN** the block's own guidance tells it to resolve before creating and to hydrate
  before duplicating

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

#### Scenario: A page the reader may not see is never named
- **WHEN** one of the pages linking the name sits in an excluded access tier, is retired,
  is an `index.md` page, or is an Entity page
- **THEN** it is neither counted nor listed in the block

#### Scenario: A name that is really a file is not a candidate
- **WHEN** two pages link a suffixed name such as `Node.js` and a file of that name
  exists in the vault
- **THEN** the response carries no block for it

#### Scenario: A warming graph sends nothing
- **WHEN** the crossing write commits while the graph index reports warming, temporary
  unavailability or quarantine
- **THEN** the response carries no block and the candidate appears in the
  `entity_recurrence` family once the graph converges

#### Scenario: A differently spelled link is the family's to count
- **WHEN** one page links `[[Harbour Studio]]` and a second page is written linking
  `[[Notes/People/Harbour Studio]]`
- **THEN** the response carries no block, and the `entity_recurrence` family produces the
  finding on its next sweep

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
yet. The shipped capture hook, capture skill and operations reference, in the scaffold
and in the plugin, SHALL permit creating an Entity for an identity that is stable and
either central to the note or recurring, SHALL NOT require recurrence, and SHALL stay
byte-identical between each source file and its packaged copy.

#### Scenario: A client with only the tool schema is told
- **WHEN** a client reads the `remember` tool's schema and nothing else
- **THEN** the `body` description tells it to link the identities the note names

#### Scenario: The capture texts allow a first-mention Entity
- **WHEN** a session wrote one note about an identity that has no Entity
- **THEN** the hook, the capture skill and the operations reference each permit resolving
  and creating it without a second mention, and none of the six shipped files still
  requires an identity to be recurring
