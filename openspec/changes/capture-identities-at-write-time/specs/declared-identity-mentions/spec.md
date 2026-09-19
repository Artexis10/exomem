## ADDED Requirements

### Requirement: Durable writes accept declared identity mentions
`remember` and `replace_memory` SHALL accept an optional `mentions` argument, and
`connect_memory` SHALL accept `operation="declare-mentions"` for an existing active
compiled page. A declaration is a list of at most 24 items, each a name or a mapping with
`name`, optional `type` and optional boolean `central`. The server SHALL store the
declaration as `mentions` frontmatter on the page and SHALL rebuild every derived
structure this capability defines from that frontmatter alone. A write without `mentions`
SHALL behave exactly as before. No server component SHALL derive a declaration from page
text. Pages inside an append-only tree SHALL never carry a declaration.

#### Scenario: A declaration is stored with the note
- **WHEN** an agent writes a note with `mentions: ["Field Recorder", {name: "Harbour
  Studio", type: "organisation", central: true}]`
- **THEN** the committed page's frontmatter holds that list and the response reports the
  declaration as accepted

#### Scenario: A write without a declaration is unchanged
- **WHEN** an agent writes a note and omits `mentions`
- **THEN** the page, the response and the graph are byte-identical to the same write
  before this capability existed

#### Scenario: Items past the cap are reported, not silently lost
- **WHEN** a declaration holds 30 items
- **THEN** the first 24 are stored and the response names the six that were dropped

#### Scenario: A declaration never fails the durable write
- **WHEN** a declaration contains an item that is not a name or a valid mapping
- **THEN** the note commits, the invalid item is omitted and the response reports it

### Requirement: Resolved mentions become typed graph edges
For each declared name the server SHALL consult the entity registry by exact name and
alias only. Exactly one match SHALL produce a `mentions` edge from the page to that
Entity, or an `about_entity` edge when the item is `central`, with origin
`declared_mention`. More than one match SHALL produce no edge and SHALL be reported with
the competing refs. The edges SHALL be removed when the declaration no longer names the
Entity.

#### Scenario: A mention of a known Entity links on the same write
- **WHEN** a note declares a name that is the alias of exactly one active Entity
- **THEN** the graph holds a `mentions` edge from the note to that Entity after the write,
  without the agent authoring a relation

#### Scenario: An ambiguous name links to nothing
- **WHEN** a declared name matches two active Entities
- **THEN** no edge is written, the response lists both refs as competing, and the name is
  not counted as an unresolved identity

#### Scenario: Removing a declaration removes its edge
- **WHEN** a page's declaration is replaced by one that omits a previously resolved name
- **THEN** the edge with origin `declared_mention` to that Entity is gone and authored
  relations to the same Entity are untouched

### Requirement: Unresolved mentions are counted by independent origin
The server SHALL maintain a derived count, per unresolved identity key, of the
independent origins whose pages declare it, where two pages compiled from the same Source
or the same session are one origin. The count SHALL be rebuildable from the vault and
SHALL never be treated as canonical.

#### Scenario: One session does not count twice
- **WHEN** two notes compiled from the same session declare the same unresolved name
- **THEN** the identity's independent-mention count is one

#### Scenario: Two independent notes count twice
- **WHEN** two notes with different origins declare the same unresolved name
- **THEN** the identity's independent-mention count is two

### Requirement: Promotion candidates at the second mention, or the first when central
An unresolved identity SHALL become a promotion candidate when its independent-mention
count reaches `promote_at_independent_mentions` (default 2), or when any declaration
marks it `central` and its count reaches `promote_central_at` (default 1). Both
thresholds SHALL be read from the vault-extensible entity-type registry, SHALL be
integers from 1 to 5, and a value outside that range SHALL be ignored with a finding.
The server SHALL never create an Entity.

#### Scenario: Second independent mention
- **WHEN** a second note with a different origin declares an unresolved name
- **THEN** the identity is a promotion candidate

#### Scenario: One central mention
- **WHEN** one note declares an unresolved name with `central: true`
- **THEN** the identity is a promotion candidate

#### Scenario: One passing mention
- **WHEN** one note declares an unresolved name without `central`
- **THEN** the identity is counted and is not a candidate

#### Scenario: The owner raises the bar
- **WHEN** the registry override sets `promote_at_independent_mentions: 3`
- **THEN** an identity declared on two independent notes is not a candidate

### Requirement: The candidate is delivered on the write that creates it
The committed response of the write that makes an identity a candidate SHALL carry an
`entity_candidate` block of at most three candidates, each with the normalised name, the
declared type cues, at most eight declaring pages, near matches from the registry, and
the routes to `resolve-entity` and `create-entity`. An identity SHALL be delivered on a
write response once per threshold crossing. The candidate SHALL also appear in the
`entity_recurrence` attention family until an Entity resolves the name or the owner
dismisses it.

#### Scenario: The agent that holds the context is told
- **WHEN** a write makes an identity cross its threshold
- **THEN** that write's response carries the candidate with its declaring pages and the
  two routes

#### Scenario: Creation closes the candidate
- **WHEN** an Entity whose name or alias matches the identity is created
- **THEN** the candidate leaves the attention family and each declaring page gains its
  edge on the next derived rebuild

### Requirement: Undeclared pages are a visible, opt-in backlog
`review_memory(mode="audit", categories=["undeclared_mentions"])` SHALL list active
compiled pages whose frontmatter has no `mentions` key, newest first and bounded. A page
whose declaration is the empty list SHALL NOT be listed. The family SHALL be opt-in and
SHALL never contribute to due-state.

#### Scenario: An adopted vault does not open in debt
- **WHEN** a vault with thousands of undeclared pages is served
- **THEN** due-state totals are unchanged by this capability

#### Scenario: Declaring nothing is a declaration
- **WHEN** a page is declared with `mentions: []`
- **THEN** it no longer appears in the `undeclared_mentions` audit
