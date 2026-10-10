## MODIFIED Requirements

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
graph index does not answer the lookup as available. Detection SHALL never create an
Entity; an Entity is created only by a leaf call or a declaration that the agent makes.

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

## ADDED Requirements

### Requirement: A write names its undeclared referents

Every committed durable page write SHALL carry an `undeclared_referents` block in its receipt when it newly wikilinks a name that resolves, in the writer's own view, to no page and no active Entity, and that no declaration of the same write covers.
The block SHALL hold at most 3 names, each with the `create-entity` route.
"Newly" SHALL be decided against the page's pre-write state, as for `entity_candidate`.
A name that the same response's `entity_candidate` block carries SHALL be omitted.
The block SHALL reuse the capture-sweep link parsing and the preflight's corpus context, with no vault walk and no model call, and SHALL read wikilink markup only, never prose.
Pages committed within one mutation batch SHALL count once, so a name that several of them newly link is listed once.
Write-advisory fingerprints SHALL dismiss a name, and a dismissed name SHALL also get no later `entity_candidate` block.
The `undeclared_referents` family SHALL be quietable.
The block SHALL be withheld when `structural_suggestions` is `off`, and when `proactive_capture` resolves to `off`, as it does at `light`.
A name that matches only a page the writer may not see SHALL be listed exactly as a name with no page.
The compact response projection SHALL carry the block, so that a hookless client receives it through MCP egress.

#### Scenario: A first link to a new name is named at once

- **WHEN** a note is written that newly links `[[Kestrel]]`, and no page or Entity answers to that name
- **THEN** the receipt's `undeclared_referents` block lists `Kestrel` with the `create-entity` route

#### Scenario: A name already linked by the page is not repeated

- **WHEN** a page that already links an unpaged name is edited and still links it
- **THEN** the receipt carries no block entry for that name

#### Scenario: An existing Entity is not a referent to declare

- **WHEN** a write newly links a name that resolves to an active Entity
- **THEN** the receipt carries no block entry for that name

#### Scenario: A withheld page is indistinguishable

- **WHEN** a restricted writer newly links a name that only a withheld page answers to, and separately a name that nothing answers to
- **THEN** both names are listed identically

#### Scenario: A quieted family stays quiet

- **WHEN** the owner quiets the `undeclared_referents` family, or `structural_suggestions` is `off`
- **THEN** no receipt carries the block

#### Scenario: A light-prominence client is not prompted

- **WHEN** prominence is `light` and `proactive_capture` resolves to `off`
- **THEN** no receipt carries the block

#### Scenario: A dismissed name is not prompted again at its second page

- **WHEN** the agent dismisses a name in the block, and a later write links the same name from a second page
- **THEN** that write's response carries no `entity_candidate` block for the name

#### Scenario: One batch lists a name once

- **WHEN** a single multi-write command commits two notes that newly link the same unpaged name
- **THEN** the block lists the name once for that batch

#### Scenario: A hookless client receives the block

- **WHEN** a hookless client calls a durable writer over MCP with compact response detail, and the write newly links an unpaged name
- **THEN** the compact response that passed MCP egress carries the block with the name and its route
