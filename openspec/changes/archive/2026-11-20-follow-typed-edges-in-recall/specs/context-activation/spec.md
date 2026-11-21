## ADDED Requirements

### Requirement: Served connections carry a reason
Activation SHALL read a page's connections through one batched read of the graph owner's
neighbour API in both directions, under the caller's view, and SHALL serve each connection with
its relation key, its direction relative to the page, the target's canonical ref and title, and a
`why`. The `why` SHALL state the relation and direction. When the caller has unrestricted
source access, it SHALL quote the prose that accompanies the link in the authoring
sentence or bullet when egress permits that quote. Otherwise it SHALL use the target's
opening clause when egress permits it. If neither supplies releasable prose, the relation
and direction stand alone. Callers whose source reads require a release projection receive
the relation and direction as the `why`. Each quote SHALL pass the egress rules that govern
unit text. A quote SHALL show a wikilink by its display name only when the
link resolves to a page the caller can see. If any link fails that check, the quote is
unavailable. The `why` SHALL contain no wikilink and SHALL NOT exceed 140 characters.
Connections SHALL be ranked only by closed relation attributes, in this order: a relation
carrying `replaces`, `corrects`, `opposes` or `settles`;
then one carrying `sources`; then any other relation that is neither `generic` nor `associative`;
then generic and associative relations. An inbound connection whose relation carries `replaces`
SHALL rank first. Within a tier, a target whose planning status class is `open` SHALL rank first,
and the owner API's order SHALL stand otherwise. A target the caller may not see SHALL never be
named, and no relation key or family name SHALL decide the order. When the graph cannot answer,
pages SHALL be served without connections and the packet SHALL report the gap with the existing
missing reasons.

#### Scenario: A replaced page names its replacement first
- **WHEN** a served page is the target of a `supersedes` edge from a newer page and also links
  three other pages with `links_to`
- **AND** the newer page links to the replaced page in prose the caller can quote
- **THEN** its first connection is the inbound `supersedes` edge to the newer page
- **AND** its `why` names the relation, the inbound direction and the newer page's sentence that
  carries the link

#### Scenario: A note leads to the source it was compiled from
- **WHEN** a served note authors `derived_from` to a source page and plain links to two notes
- **THEN** the `derived_from` connection ranks before both plain links

#### Scenario: A withheld page is never a connection
- **WHEN** a restricted caller's served page links a page withheld from that caller
- **THEN** no connection names the withheld page and no `why` quotes text that names it

#### Scenario: Graph unavailable
- **WHEN** the graph cannot answer during activation
- **THEN** leads and anchors are served without connections and the packet reports the gap

### Requirement: A lead keeps its first connection
Task discovery SHALL admit each lead together with its first ranked connection when both fit the
allowance. When only the lead fits, it SHALL be served without connections and the candidate
SHALL count the connections it omits. A lead's own methods SHALL outrank its first connection: when
a method does not fit beside the connection, the connection waits and returns only if room remains.
A first connection MAY take the room of a later lead. Further connections SHALL fill the room that
remains after every lead and method, in lead order. The connection line SHALL tell the agent to read a
connection when its reason bears on the answer. The served-context record SHALL keep each offered
connection's relation, target ref and tier, with no title or `why`.

#### Scenario: A lead and its first connection are admitted together
- **WHEN** the allowance holds four leads with one connection each, or six leads without
  connections
- **THEN** four leads are served, each with its first connection, and the remaining leads are
  reported as cut for budget

#### Scenario: A lead's method outranks its first connection
- **WHEN** the allowance holds a lead with either its method or its first connection, not both
- **THEN** the lead is served with its method, and the connection is served only if room remains

#### Scenario: The record names what was offered
- **WHEN** a packet serves a lead with two connections
- **THEN** the served-context record lists both connections' relation, target ref and tier

### Requirement: Resolved anchors serve their connections
For each resolved anchor, activation SHALL serve up to four of the anchor page's ranked
connections as `pointers[]` entries (ref, title, why) through the `graph` lane. The anchor's
neighbourhood for the other lanes SHALL be traversed under the `recall` traversal profile at
depth at most 2, within the existing node and edge bounds, and under the reader's release decision:
a page the reader cannot see is never a hop, so a restricted packet is identical whether or not
that page exists. A connection pointer's reason is `requires_read`.

#### Scenario: An anchor names its typed connections
- **WHEN** a turn resolves an entity anchor that one note `refines` and that links its owner's
  page with `about_entity`
- **THEN** the packet's `pointers[]` lists both, each with a `why`, and the `refines` connection
  ranks first

#### Scenario: A plainly linked note reaches the unit lanes
- **WHEN** a resolved anchor links a note only through `links_to`
- **THEN** that note is inside the anchor neighbourhood the unit lanes read

## MODIFIED Requirements

### Requirement: Read-only activation operation
The product SHALL expose a read-only operation `activate_context` that accepts a raw
user turn (`turn`, non-empty text), an optional `max_chars` (default 6000, clamped
to 500..8000), an optional declared `purpose`, and an optional `include_timings`
flag, and returns a working-memory packet. The operation SHALL be reachable over the
same leaf function on MCP, the CLI (`exomem activate "<turn>"`) and the personal REST
facade (`/api/activate_context`). It SHALL perform no vault write, SHALL NOT change the
hits, ordering or envelope of `ask_memory`/`find` for any input, and SHALL run no model
other than the retrieval scorers ordinary recall already runs.

#### Scenario: Same packet on every door
- **WHEN** the same turn is submitted through MCP, the CLI and the REST facade against
  the same vault state and index generation
- **THEN** the three responses carry the same anchors, roles, units, pointers and
  budget accounting

#### Scenario: Ordinary recall is untouched
- **WHEN** `ask_memory` is called with any arguments before and after this change is
  installed and the activation index exists
- **THEN** its hits, ordering and envelope are byte-identical

#### Scenario: Kill switch abstains without building
- **WHEN** `EXOMEM_DISABLE_WORKING_SET` is set in the server process
- **THEN** `activate_context` returns a packet with `abstained: true` and
  `abstention.reason = "disabled"`, no activation index is created or opened, and the
  tool remains on the surface so the tool-surface digest is independent of the switch
