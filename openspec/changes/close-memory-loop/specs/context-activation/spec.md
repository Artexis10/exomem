## MODIFIED Requirements

### Requirement: Read-only activation operation
The product SHALL expose a read-only operation `activate_context` that accepts a raw
user turn (`turn`, non-empty text), an optional `max_chars` (default 4000, clamped
to 500..8000), an optional declared `purpose`, an optional `include_timings`
flag, and optional `client` and `session` attribution, and returns a working-memory
packet. The operation SHALL be reachable over the
same leaf function on MCP, the CLI (`exomem activate "<turn>"`, with `--client` and
`--session`) and the personal REST facade (`/api/activate_context`). It SHALL perform
no vault write, SHALL NOT change the
hits, ordering or envelope of `ask_memory`/`find` for any input, and SHALL run no model
other than the retrieval scorers ordinary recall already runs.

Each call SHALL append one row to a host-local activation log beside the existing query
logs, outside the vault. The row SHALL carry the client label the transport observed or,
failing that, a declared `client` matching a short software-label pattern, a stable
per-vault hash of `session` rather than its value, a hash identifying the vault, the transport, the outcome and
packet-derived counts. It SHALL NOT carry the turn text, and SHALL omit anchor
identifiers when the packet is content-private. An invalid `client` or an oversize
`session` SHALL be recorded as invalid and never refused or echoed. `client` and
`session` SHALL NOT change the packet's material. `session` MAY select the caller whose
session start carries the optional `upkeep` block that the adaptive-memory-maintenance
capability defines, and nothing else. On the MCP door only, while
proactive capture is permitted, the packet MAY carry a one-sentence `episode_due`
advisory after repeated activations from one caller without an episode record, at most
once per bounded interval; it SHALL NOT appear on the CLI or REST doors, nor for a
Claude Code or Codex MCP client whose hook this vault's activation log shows served it
within that interval.

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

#### Scenario: Attribution never changes the packet
- **WHEN** the same turn is submitted against the same vault state with and without
  `client` and `session`, or with an invalid label or oversize session
- **THEN** the packets are identical apart from the continuity token each call mints,
  the optional `upkeep` block, whose session start the `session` identifies, and
  `budget.used_chars`, which counts that block's item when one is attached
- **AND** the call is never refused over its attribution

#### Scenario: The turn is never recorded
- **WHEN** an activation is logged
- **THEN** its row carries neither the turn text nor the raw session value
- **AND** a content-private packet's row carries no anchor identifiers

## ADDED Requirements

### Requirement: Competing senses decided by the turn's own words
Resolution SHALL let the turn's own words decide between same-kind senses in two cases
beyond the resolved-anchor ambiguity rule. First, a bare shared name: when no anchor
resolves and two or more entity anchors are `partial` on one and the same authored name
word alone (`rare_term`, with qualifiers at most), and that word is said as a name, the
turn SHALL be `ambiguous` between those entities, formed by the same anchor-neighbourhood
connectivity rule as any competing group, and the retrieval carry SHALL NOT be asked. A
word is said as a name when every such entity is a person (by its own `entity_type`,
which the activation index records), or, in a cased script, when the turn capitalises
the word somewhere other than at a sentence start; in an uncased script only the person
case applies. A single such entity SHALL stay a `partial` lead, and a shared word in the
names of two anchors of any other kind SHALL NOT form this ambiguity. Second, a
qualifier: each anchor's contact SHALL be the longest contiguous run of turn tokens that
spells its own authored name words (stopwords may sit inside a run, never at its edges;
punctuation and coordinators end a contiguous run: a sentence end, comma, colon,
semicolon or dash, and "and" or "or", so "X, Y" and "X and Y" are two things, not one
run).
When two same-kind anchors resolve without a deciding-alone kind and one's run lies
strictly inside the other's, the narrower anchor SHALL NOT be listed, and neither SHALL a
same-kind `partial` anchor with no retrieved contact whose run lies strictly inside the
chosen anchor's run. A word of the wider name said outside that run SHALL narrow nothing,
and where no run strictly contains another every sense the turn reached SHALL stay
listed. Neither rule SHALL compare anchors of different kinds.

#### Scenario: A bare first name two people share is a question
- **WHEN** a turn says only a first name that two unlinked person entities share, and
  nothing in the turn resolves, even though the turn also names a carryable note
- **THEN** the packet is `ambiguous`, lists both people under `ambiguity`, and carries
  no retrieved page

#### Scenario: One person with that name stays a lead
- **WHEN** the same turn reaches a single person entity on that name word
- **THEN** the packet abstains `unresolved` with that entity as a `partial` anchor

#### Scenario: An ordinary noun two businesses are named after is not a name
- **WHEN** a turn says, in lower case or only as a sentence's first word, a word that
  two organisation entities' names share
- **THEN** the turn is not `ambiguous` between them, and the retrieval carry may still
  serve a page the turn names

#### Scenario: A qualifier names one sense
- **WHEN** a turn resolves two same-kind hubs and spells, in one contiguous run, the
  shared name words together with a word only one hub's name carries
- **THEN** that hub resolves alone, the other hub is not listed, and a same-kind
  partial hub reached only inside that run is not listed

#### Scenario: Punctuation and coordinators end a contiguous run
- **WHEN** a turn puts a comma, full stop, colon, dash, "and" or "or" between the shared
  name words and the word only one hub's name carries
- **THEN** the run does not span it and the packet is `ambiguous` between the hubs

#### Scenario: A detached word of the wider name narrows nothing
- **WHEN** the word only one hub's name carries appears elsewhere in the turn, apart
  from the run that spells the shared words
- **THEN** the packet is `ambiguous` between the hubs

#### Scenario: The shared words alone stay ambiguous
- **WHEN** a turn says only the words both hub names share
- **THEN** the packet is `ambiguous` and every hub the turn reached stays listed

### Requirement: A unit the anchor lede already says is served once
When the packet carries a resolved anchor's own page at page level (the identity lede,
by the page's own ref), a role-lane unit fragment of that same page whose whole text the
lede already contains SHALL be omitted, so the sentence is neither printed nor charged
twice nor reported as a second reference. Any other unit of that page, and the same text
on any other page, SHALL be kept.

#### Scenario: A resource's lede observation is not repeated as a unit
- **WHEN** a resolved resource's identity lede opens with an observation that a role
  lane also selects as a unit of that page
- **THEN** the packet carries the lede once, by the page's ref, and no unit fragment of
  that page repeating it

### Requirement: A Planning item anchor reports its item page
Each active Planning item's `plan` anchor SHALL report the item's own canonical page as
its ref, and the active-plans lane SHALL spell the item by that ref, so a turn that
resolves to one intended item is served that item rather than the collection manifest
it is filed under or an internal item id. The collection SHALL remain the anchor's home:
items filed in one collection SHALL stay complementary, and an agent choice naming the
collection SHALL still select every item in it. When the item's page cannot be read,
the anchor SHALL report its home as before.

#### Scenario: A turn about one item is served that item
- **WHEN** a turn names one active item of a Planning collection that holds several
- **THEN** the resolved `plan` anchor's ref and the active-plans unit are the item's
  own page, and neither names the collection manifest

#### Scenario: Items filed together stay complementary
- **WHEN** a turn resolves two items of one Planning collection
- **THEN** the turn is not `ambiguous` between them

### Requirement: Retired revisions do not make a page's name ordinary
The retrieval carry SHALL measure how distinctive a turn's word is without counting
retired pages, by the same test the carry applies to its own candidates: a page whose
own status retires it, or whose `superseded_by` names its replacement. A page revised
several times thus stays nameable by the words its retired revisions share. The
`superseded_by` test MAY be applied only to a word counted at most one carry window
above the distinctiveness cap, since no other word can become distinctive. Current pages
SHALL still count, and the corpus page total SHALL be unchanged.

#### Scenario: A page revised three times is still carried
- **WHEN** a turn names a page by a phrase that its three superseded revisions also
  carry, and no other current page carries it
- **THEN** the carry admits the current page alone

#### Scenario: A revision retired only by its successor pointer is not counted
- **WHEN** the three revisions keep `status: active` but each names the current page in
  `superseded_by`
- **THEN** the carry still admits the current page alone

#### Scenario: Current namesakes still make a phrase ordinary
- **WHEN** four current pages carry the same phrase
- **THEN** the phrase is not distinctive and no single page is carried
