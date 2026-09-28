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
word alone (`rare_term`, with qualifiers at most), the turn SHALL be `ambiguous` between
those entities, formed by the same anchor-neighbourhood connectivity rule as any
competing group, and the retrieval carry SHALL NOT be asked; a single such entity SHALL
stay a `partial` lead, and a shared word in the names of two anchors of any other kind
SHALL NOT form this ambiguity. Second, a qualifier: when two same-kind anchors resolve
without a deciding-alone kind and the authored name words the turn reached on one are a
strict subset of those it reached on the other, the narrower anchor SHALL NOT be listed,
and neither SHALL a same-kind `partial` anchor reached only through a strict subset of
the chosen anchor's name words; where no such subset exists every sense the turn reached
SHALL stay listed. Neither rule SHALL compare anchors of different kinds.

#### Scenario: A bare first name two people share is a question
- **WHEN** a turn says only a first name that two unlinked person entities share, and
  nothing in the turn resolves
- **THEN** the packet is `ambiguous`, lists both people under `ambiguity`, and carries
  no retrieved page

#### Scenario: One person with that name stays a lead
- **WHEN** the same turn reaches a single person entity on that name word
- **THEN** the packet abstains `unresolved` with that entity as a `partial` anchor

#### Scenario: A qualifier names one sense
- **WHEN** a turn resolves two same-kind hubs on their shared name words and also says
  a word only one hub's name carries
- **THEN** that hub resolves alone, the other hub is not listed, and a same-kind
  partial hub reached only through words of the chosen hub's name is not listed

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
pages whose own status retires them (the statuses the carry already refuses to serve),
so a page revised several times stays nameable by the words its retired revisions
share. Current pages SHALL still count, and the corpus page total SHALL be unchanged.

#### Scenario: A page revised three times is still carried
- **WHEN** a turn names a page by a phrase that its three superseded revisions also
  carry, and no other current page carries it
- **THEN** the carry admits the current page alone

#### Scenario: Current namesakes still make a phrase ordinary
- **WHEN** four current pages carry the same phrase
- **THEN** the phrase is not distinctive and no single page is carried

### Requirement: A turn that names several domains is served all of them
Activation SHALL compile the smallest sufficient SET of concurrently relevant contexts,
and the count SHALL be driven by relevance, never capped at one. Each domain a turn
explicitly names, with a resolvable page, is a candidate in its own right. Competing
senses are anchors the turn's SAME words reach: a same-kind anchor the turn spelled by
its own name, in words that no other same-kind anchor was reached through, SHALL NOT be
listed as a sense of them and SHALL NOT make the turn `ambiguous`. Two anchors reached
through a shared spelled word SHALL still compete exactly as before.

#### Scenario: Two same-kind domains named apart are both served
- **WHEN** a turn spells the names of two unlinked hubs in disjoint words
- **THEN** both resolve, the turn is not `ambiguous`, and both are served

#### Scenario: One shared name two hubs carry is still a question
- **WHEN** a turn says only the words two same-kind hubs share
- **THEN** the turn is `ambiguous` between them, as before

#### Scenario: A domain that is an ordinary page is served beside the resolved anchor
- **WHEN** a turn resolves an anchor and also names, by a distinctive phrase of its own,
  a current ordinary page that is none of that anchor's neighbourhood
- **THEN** the packet also carries that page as an anchor of kind `page` at status
  `retrieval_carried`, marked `generation.also_carried = "retrieval"`, with its units;
  the resolved anchor is unchanged and the page is never reported `resolved`
- **AND** a turn that names nothing beyond what it resolved carries nothing extra

#### Scenario: Several pages named apart are each carried
- **WHEN** a turn that resolved no anchor names two pages by two phrases, each phrase
  answering to exactly one page
- **THEN** both pages are carried, bounded at three by score, and a phrase two pages
  answer to is not carried and is never guessed between

### Requirement: Same-thread pages are candidate anchors for an overlapping turn
For a caller with its own session or thread, a turn that resolved no anchor and is not
answered by the retrieval carry SHALL treat the pages that conversation's own tier holds
(the pages listed in its `recent_context`) as candidates, gated by overlap with the
turn: the turn shares at least two words of the page's own name (title, aliases, tags),
or all of a shorter name, or at least three words of its body. A qualifying page SHALL
be served as a `partial` anchor with its units, marked `generation.carried_by =
"follow_up"`; two or more qualifying pages SHALL abstain `ambiguous`, listing them. A
turn sharing nothing with the page is not about it, another conversation's work and a
keyless caller's vault-wide heat are never asked, and nothing is ever resolved this way.

#### Scenario: A follow-up in the page's own words is served the page
- **WHEN** a keyed conversation worked on an ordinary page, and its next turn shares
  two words of that page's title but names no anchor and forms no carry phrase
- **THEN** the packet serves that page as a `partial` anchor with `carried_by:
  "follow_up"` instead of abstaining `unresolved`

#### Scenario: An unrelated follow-up is not served the thread
- **WHEN** the same conversation's next turn shares nothing of the page's name or body
- **THEN** the page is not served

### Requirement: A resolved entity is served the conclusions linked to it
An entity anchor SHALL be read through the precedents lens by default, so that the
`decision`, `insight` and `finding` units of the pages typed-linked to the entity are
served with their own provenance (category, parent page, supersession), inside the
existing per-lane and packet budgets. A conclusion note that is not linked to the
entity SHALL NOT be served on its account, and a superseded conclusion SHALL be marked
as such, never presented as current.

#### Scenario: A settled decision about an organisation is served with the entity
- **WHEN** a turn names an organisation entity and a decision note links to that entity
- **THEN** the packet serves the decision unit with `provenance.category: "decision"`
  and its parent page, and serves no decision note that does not link to the entity

### Requirement: A carried page is read through the lenses its own units answer
The retrieval carry SHALL read each carried page through the `units` lenses that select
a category the page's own units are filed under, ordered by the turn's cues and then by
priority and bounded by the same lens ceiling as any packet, so a page whose material
sits under a category only a later lens selects is not read as empty. Where the page's
categories cannot be read, every `units` lens applies as before.

#### Scenario: A named page holding only a late lens's category is served
- **WHEN** a turn names a current ordinary page by a distinctive phrase, and the page's
  only units are filed under a category selected by a lens beyond the first six by
  priority
- **THEN** the packet carries the page with those units instead of abstaining
