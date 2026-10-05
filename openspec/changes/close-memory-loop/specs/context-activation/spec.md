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

### Requirement: Categorical anchor evidence and resolution
Anchor candidates SHALL carry only categorical evidence kinds. Contact kinds —
`exact_alias`, `lexical_overlap`, `vector_band`, `claims_match` and `retrieval` —
establish that the turn reached the anchor; qualifier kinds — `category_match`,
`graph_corroboration` and `usage_prior` — strengthen an anchor the turn already reached
and SHALL never create a candidate on their own. No float score SHALL appear in the
packet. An anchor SHALL resolve as `resolved` when it carries `exact_alias` or at least
two independent kinds other than `usage_prior`, at least one of them a contact kind; as
`partial` when it carries exactly one kind other than `usage_prior`; the turn SHALL be
`ambiguous` when two or more resolved anchors of the same anchor kind share no anchor
neighbour, neither is a neighbour of the other, and the turn reached them through the
same words (a same-kind anchor the turn spelled by its own name, in words that no other
same-kind anchor was reached through, is a second topic the turn also named, never a
sense of the others), where an anchor's anchor
neighbourhood is the set of its typed-link neighbours that are themselves anchors in
the activation index (resolved anchors of different kinds are complementary; a shared
page that is not an anchor, reached by alias or otherwise, never makes two anchors
complementary; a direct typed link between the two always does; project-key anchors
have no page, so they can neither bridge two anchors nor be anyone's neighbour, and two
resolved project anchors are therefore trivially competing); otherwise,
when no anchor is `resolved`, the turn SHALL be `unresolved` and the operation SHALL
abstain with an empty packet. Lexical overlap SHALL ignore stopwords, and turn tokens
SHALL keep their order and repetitions for n-gram construction so that two anchors
sharing a word in their names can both receive `exact_alias` from one turn. `usage_prior` SHALL only break ties between
otherwise equal candidates and SHALL never contribute to the two-kinds rule.
A path or URL quoted in a turn SHALL be one reference, not words: a rooted path (`/`,
`\\`, `~/` or a drive letter) or a URL SHALL contribute no term to any subject
evidence, and a relative path SHALL contribute only its final segment, so a quoted
vault page path still names that page as its file name always did. Whether a turn only
points back or is a follow-up SHALL still read the turn as written.
`claims_match` SHALL be computed with the existing collection-claims routing and
`graph_corroboration` SHALL count a typed edge between two candidates even when both
already appear in ordinary recall.

Sense adjudication SHALL precede candidate/admission truncation and use the complete
matched set from the caller-visible maintained anchor index, whose existing bound
is 2,000 identities. Conversation/focus qualification SHALL NOT truncate that set
before adjudication or infer a unique referent from truncated alternatives. This
is completeness within the maintained index, not a complete-vault claim. After
adjudication, admission SHALL retain resolved anchors before partial anchors,
preserving rank within each group and the ordinary six-anchor allowance; additional
resolved anchors explicitly named by current-turn `exact_alias` or selected through
the existing agent-choice route MAY expand admission up to 24 anchors. Those extra
anchors consume the ordinary allowance first. Partial, focus-only, recency and
implicit contact SHALL NOT earn extra capacity. Displayed ambiguity SHALL be
bounded independently without changing the full-set verdict. Capacity omission
SHALL use section-level `missing` reasons without omitted names or hidden counts,
derived only from the caller's released view.

#### Scenario: A competitor beyond an old cut remains a competitor
- **WHEN** a caller-visible indexed alternative beyond the former six-anchor or
  24-candidate cut changes the existing sense adjudication
- **THEN** the full matched-set verdict remains unresolved or ambiguous as applicable,
  even when displayed choices are limited; admission never manufactures uniqueness

#### Scenario: Seven distinct named contexts can be admitted
- **WHEN** a current turn explicitly names seven independently resolved indexed
  contexts and the existing rules find no competing senses
- **THEN** admission can retain all seven within its 24-anchor ceiling, while
  partial, focus-only and implicit candidates receive no extra allowance

#### Scenario: Partial retrieval hits do not erase resolved contexts
- **WHEN** six partial retrieval candidates rank ahead of two independently resolved
  connected lexical candidates after full-set adjudication
- **THEN** ordinary admission retains the resolved candidates and runs their eligible
  lanes instead of returning a resolved packet containing only partial anchors

#### Scenario: Two kinds resolve a resource anchor
- **WHEN** a turn mentions a resource whose profile page title matches lexically and
  whose Records collection claims cover the turn's terms
- **THEN** the anchor resolves with evidence `[lexical_overlap, claims_match]`

#### Scenario: Ambiguous domain is reported, not guessed
- **WHEN** a turn's terms resolve two hub anchors whose anchor neighbourhoods share no
  anchor
- **THEN** the packet status is `ambiguous`, both anchors are listed under
  `ambiguity` with their neighbourhood sizes, and no role lane runs for either

#### Scenario: Two directly linked anchors are complementary, not competing
- **WHEN** a turn resolves two same-kind anchors and one of them links the other
- **THEN** the packet is not `ambiguous`, both anchors are served, and both carry
  `graph_corroboration`

#### Scenario: A shared boilerplate page does not suppress ambiguity
- **WHEN** two same-kind resolved anchors both link one page that is not an anchor,
  for example a handbook reached through a short alias, and share no anchor neighbour
- **THEN** the packet status is still `ambiguous`

#### Scenario: Two names sharing a word both resolve
- **WHEN** a turn names two anchors whose names share a word, such as "Alpha
  Initiative and Beta Initiative"
- **THEN** both anchors carry `exact_alias`

#### Scenario: Negative twin abstains
- **WHEN** a turn is lexically similar to an anchor's domain but carries no alias, no
  claims coverage and no corroborating kind
- **THEN** no anchor is `resolved`, the packet is `abstained: true` with
  `abstention.reason = "unresolved"`, and `units` and `pointers` are empty

#### Scenario: Usage never resolves
- **WHEN** a candidate carries only `usage_prior` and `vector_band`
- **THEN** it is at most `partial`

#### Scenario: A quoted path names no project
- **WHEN** a turn quotes `/home/<user>/handoffs/<file>.md`, a `~/` path, a Windows
  drive path or a URL whose segments spell a project key such as `home` or `records`
- **THEN** no project anchor is reached through those segments and none of its material
  is served, while a turn naming the same project in prose still resolves it

#### Scenario: Two same-kind domains named apart are both served
- **WHEN** a turn spells the names of two same-kind anchors that share no anchor
  neighbour, in words neither name shares
- **THEN** the packet is not `ambiguous` and both anchors are served

#### Scenario: One shared name two same-kind anchors carry is still a question
- **WHEN** a turn says only words two unlinked same-kind anchors share
- **THEN** the packet status is `ambiguous`, as before

## ADDED Requirements

### Requirement: Relevant open-category material stays reachable
A selected material lane SHALL serve query-matched compiled knowledge only within
already admitted caller-visible contexts, without creating resolution evidence.
It SHALL exclude categories owned by any effective non-material role, selected or not,
using each parent's scoped category identity and accepted aliases. Unknown labels
SHALL retain their literal identity; the server SHALL NOT infer a category mapping.

#### Scenario: A relevant open category is not lost behind newer unrelated units
- **WHEN** an admitted project has an older relevant unit in an otherwise unowned category
  and newer unrelated units
- **THEN** material can carry the relevant unit with its authored category and provenance,
  without ranking standing role lanes by the turn or serving out-of-context knowledge

#### Scenario: A scoped owner mapping cannot be bypassed
- **WHEN** an owner role uses an accepted category alias for one project, and another
  project uses the same spelling with a different identity
- **THEN** material respects the first mapping even when that role is unselected,
  without applying its exclusion to the other project's distinct category

### Requirement: Prose pointers do not pretend to be compiled units
Material MAY offer a relevant compiled-page pointer when authored prose is not covered
by semantic units, including on mixed-content pages. Its reason SHALL state that the
page requires reading. The compiler SHALL NOT fabricate a unit or present an arbitrary
prose excerpt as a semantic claim.

#### Scenario: Relevant mixed prose remains inspectable
- **WHEN** a relevant page's few semantic units do not contain the needed prose
- **THEN** a source pointer can identify the page without claiming that the missing
  prose was compiled into the packet

### Requirement: Material lookup remains bounded and honestly partial
Material SHALL use maintained catalogues without foreground repair, corpus scanning
or model acquisition. Context and category predicates SHALL precede its read cap:
200 unit candidates plus a sentinel and three pointer candidates plus a sentinel.
The shared material allowance SHALL remain three items by default and MAY expand
only for additional explicit-request coverage under the following requirement.
Its unit and prose-page lookups SHALL share a finite material query-unit and stem
budget independent of ordinary anchor resolution. This budget SHALL retain current
turn evidence for concurrently admitted topics without changing context admission,
categorical ownership, ranking or packet bounds. Exhaustion SHALL NOT trigger an
unbounded retry, corpus walk, new model call or whole-lane refusal.
Expanding the allowance SHALL NOT disable the existing corpus-common word filter
on medium-length turns; its short-query threshold remains independently applicable.
Existing packet budgets, currentness and egress rules SHALL apply. Empty, unavailable,
truncated and budget-limited outcomes SHALL remain distinct, including carried pages.

#### Scenario: An unavailable or capped catalogue is not an empty project
- **WHEN** material's catalogue is unavailable or its candidate window is exhausted
- **THEN** the packet reports the applicable lane failure or truncation rather than
  claiming no material, and performs no fallback walk or model load

#### Scenario: Earlier incidental discussion does not erase admitted topics
- **WHEN** a bounded long turn includes incidental discussion and two independently
  admitted topics with useful authored conclusions, in either topic order
- **THEN** the material query can retain the evidence for both conclusions and serve
  their provenance within the unchanged candidate and packet limits

#### Scenario: Long and mixed-script queries retain finite work
- **WHEN** a turn exceeds the material query-unit or stem allowance, including a
  long unspaced run beside spaced words
- **THEN** the existing selector stays within both finite bounds, ordinary resolution
  retains its own budget, and no fallback broadens the admitted contexts or packet

### Requirement: Explicit named requests share bounded material capacity
Within the existing admitted and caller-visible candidate pool, material SHALL
prefer representing unrepresented current-turn requests that name exactly one
resolved anchor and retain non-name query evidence in a sentence or semicolon
segment. Separators inside an admitted literal name SHALL NOT split that request.
Equivalent repeated requests SHALL NOT gain allocation weight. Bare names,
multi-anchor segments and focus-only contact SHALL retain the existing fallback.

Coverage SHALL require matching retained query evidence in actual unit content
or uncovered authored prose within that anchor's base material neighbourhood.
Titles, covered unit text and later role reach SHALL NOT substitute for that
evidence. Equivalent matches SHALL prefer compiled units; remaining capacity
SHALL use existing material preference and rank. Packet assembly SHALL preserve
the combined selected order of units and pointers within existing role priority,
character budgets and the conditional shared material allowance. Earlier categorical roles, including their pages
and downgraded pointers, SHALL spend before a material group at its declared
registry position; later roles SHALL follow. Categorical portions SHALL retain
unit-first ordering, and packets without material SHALL retain the old ordering.
Own/promoted tiers and current-state/Planning precedence SHALL remain unchanged.
Beyond the default three items, each additional served material item SHALL represent
at least one further distinct resolved anchor with an eligible current-turn request
that prior served material has not represented. Multiple or repeated questions about
one anchor SHALL NOT multiply its extra allowance. An item covering several anchors
SHALL NOT create spare capacity for unrelated material. Shared enforcement SHALL
span all selected material roles, compiled units and prose pointers; private
request-coverage state SHALL NOT enter serialized provenance. A pointer represents
an inspectable source, not a delivered claim. Categorical item caps, the six-role
ceiling and the total prose budget SHALL remain unchanged.

It SHALL NOT add searches, traversals or model calls, or claim recovery beyond the
existing candidate windows. Complete serialized packet bytes and labelled reference
token estimates SHALL be measured separately from prose characters; an unchanged
prose budget SHALL NOT be presented as unchanged context overhead.

#### Scenario: Additional explicit domains use existing eligible material
- **WHEN** a turn has four or seven distinct admitted named requests, each with
  matching compiled evidence already in the maintained candidate pool, in either order
- **THEN** material can represent each within the total prose budget without reserving
  slots for bare names, unsupported requests or unrelated content

#### Scenario: Candidate-window loss does not become fabricated coverage
- **WHEN** an admitted request's useful prose or units did not reach the capped pool
- **THEN** extra served capacity does not invent a claim or pointer, candidate
  truncation stays visible and no complete-answer or full-topic coverage is claimed

#### Scenario: Distinct claims do not consume a second named question's slot
- **WHEN** one admitted topic has three distinct matching units and a separately
  named question has matching uncovered prose in another admitted topic
- **THEN** material can represent both requests within three items in either
  question order, without selecting the second topic's unrelated prose merely
  because it repeats words from the first question

#### Scenario: A bare name or focus offset does not reserve capacity
- **WHEN** another anchor is only named without non-name request evidence, or its
  contact occurs only in focus at coordinates shared with the current turn
- **THEN** it gains no request-allocation entitlement and existing relevance,
  admission and fallback behaviour remain unchanged

#### Scenario: One topic retains the full material allowance
- **WHEN** one named request has three relevant distinct compiled units
- **THEN** all three remain eligible without a reserved pointer slot, and missing
  or truncated evidence is not represented as a complete answer

#### Scenario: Earlier categorical context survives a tight budget
- **WHEN** a tight packet can afford either earlier identity or constraint
  context, including a downgraded pointer, or lower-priority material
- **THEN** the earlier role retains capacity, while later categorical units do
  not jump ahead of the combined material group solely because they are units

### Requirement: Project configuration participates in activation freshness
A project-registry-only edit SHALL be visible to ordinary writer validation and
invalidate affected warm activation state without a release or restart. Managed
refresh SHALL use the existing background path and report stale or warming state
until the current catalogue is usable, with no request-thread encoder acquisition.
Existing background resource policy SHALL remain unchanged. A cached tool-description list SHALL NOT
override the live registry.

#### Scenario: A YAML-only project addition reaches the warm compiler
- **WHEN** a project key is added while Markdown and the activation catalogue are unchanged
- **THEN** writer validation sees it immediately, activation reports any refresh lag
  honestly, and the refreshed catalogue can resolve the new key without a restart

### Requirement: Competing senses decided by the turn's own words
Resolution SHALL let the turn's own words decide between same-kind senses in two cases
beyond the resolved-anchor ambiguity rule. First, a bare shared name: when no anchor
resolves and two or more entity anchors are `partial` on one and the same authored name
word alone (`rare_term`, with qualifiers at most), and that word is said as a name, the
turn SHALL be `ambiguous` between those entities, formed by the same anchor-neighbourhood
connectivity rule as any competing group, and the retrieval carry SHALL NOT be asked. A
word is said as a name when every such entity is a person (by its own `entity_type`,
which the activation index records), unless the turn's casing says something and it
wrote that cased-script word in lower case, or, in a cased script, when the turn
capitalises the word somewhere other than at a sentence start; in an uncased script only
the person case applies. A turn's casing says something only when it mixes capitalised
and lower-case words: an all-lower-case turn, an all-caps turn and a headline whose every
word is capitalised carry no casing signal, so no non-person group forms in them and
persons keep their all-lower-case behaviour. A single such entity SHALL stay a `partial` lead, and a shared word in the
names of two anchors of any other kind SHALL NOT form this ambiguity. Second, a
qualifier: each anchor's contact SHALL retain the longest contiguous run of turn tokens that
spells its own authored name words (stopwords may sit inside a run, never at its edges;
punctuation and coordinators end a contiguous run: a sentence end, comma, colon,
semicolon, parenthesis, square bracket or dash, and "and" or "or", so "X, Y" and "X and Y" are two things, not one
run). A separator inside a complete title or alias already admitted to the caller-visible
activation index and spelled literally in the
turn SHALL belong to that name's occurrence, not end its run. This exception SHALL NOT
add a contact evidence kind, change derived-alias admission, or apply to a token sequence
whose name punctuation was replaced with a different clause boundary. Equality with a
possible derived short name SHALL NOT exclude an already admitted alias: indexed aliases
do not certify whether that spelling was authored or derived.
Occurrence tracking SHALL also retain every run with at least two name-word tokens when
such a run exists, and otherwise every one-word name run. An isolated generic word after
a multi-word name SHALL NOT count as a separately named sense. These internal run counts
SHALL NOT create contact evidence or appear in the packet.
When two same-kind anchors resolve without a deciding-alone kind and one's run lies
strictly inside the other's, the narrower anchor SHALL NOT be listed only when every
qualifying occurrence is inside a wider name run. Neither SHALL a
same-kind `partial` anchor with no retrieved contact whose run lies strictly inside the
chosen anchor's runs at every qualifying occurrence. A word of the wider name said outside that run SHALL narrow nothing,
and where no run strictly contains another every sense the turn reached SHALL stay
listed. Neither rule SHALL compare anchors of different kinds or span coordinates
from different turn/focus segments. An already turn-reached candidate SHALL retain
its turn occurrence authority when focus also reaches it.

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

#### Scenario: A lower-case person word in a cased turn is a word
- **WHEN** a turn that mixes capitalised and lower-case words says a person entity's
  shared first name only in lower case ("Please mark the task done")
- **THEN** the turn is not `ambiguous` between the people who share that name

#### Scenario: A headline carries no casing signal
- **WHEN** every word of a turn is capitalised, or the turn is all caps, and it says a
  word two organisation entities' names share
- **THEN** the turn is not `ambiguous` between them

#### Scenario: A qualifier names one sense
- **WHEN** a turn resolves two same-kind hubs and spells, in one contiguous run, the
  shared name words together with a word only one hub's name carries
- **THEN** that hub resolves alone, the other hub is not listed, and a same-kind
  partial hub reached only inside that run is not listed

#### Scenario: Punctuation and coordinators end a contiguous run
- **WHEN** a turn puts a comma, full stop, colon, dash, parenthesis, bracket, "and" or "or" between the shared
  name words and the word only one hub's name carries
- **THEN** the run does not span it and the packet is `ambiguous` between the hubs

#### Scenario: A detached word of the wider name narrows nothing
- **WHEN** the word only one hub's name carries appears elsewhere in the turn, apart
  from the run that spells the shared words
- **THEN** the packet is `ambiguous` between the hubs

#### Scenario: A complete authored compound name keeps its separator
- **WHEN** a turn spells a complete hub title or admitted alias containing a coordinator or
  comma and otherwise resolves both that hub and a weaker same-kind namesake
- **THEN** the spelled title is one contiguous name run and its internal separator
  does not cause a false ambiguity with the weaker namesake
- **AND** a sentence boundary replacing the authored comma does not qualify that run

#### Scenario: A later independent shorter mention survives qualification
- **WHEN** a turn spells a complete compound name and separately states a shorter
  competing multi-word name later in the turn
- **THEN** the shorter anchor remains listed and the genuine competition stays ambiguous
- **AND** repeating only the complete compound name does not name its weaker competitor

#### Scenario: An authored alias may share a derived spelling
- **WHEN** an authored alias equals its title's eligible derived short name and the
  persisted index admits that spelling once
- **THEN** a complete literal occurrence receives the same separator handling as any
  other admitted alias, without requiring authorship metadata or adding an alias

#### Scenario: A turn qualifier does not consume an unrelated focus lead
- **WHEN** a complete compound name narrows competing hubs in the turn segment and
  focus independently reaches an unrelated same-kind partial lead
- **THEN** that lead remains listed with focus origin even when its focus-local
  span coordinates fit numerically inside the turn's compound span
- **AND** adding a filler word to focus does not remove the lead

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

### Requirement: A turn that names several domains is served all of them
Activation SHALL compile the smallest sufficient SET of concurrently relevant contexts,
and the count SHALL be driven by relevance, never capped at one. Each domain a turn
explicitly names, with a resolvable page, is a candidate in its own right: same-kind
anchors named apart are served together (see the modified resolution requirement), and
an ordinary page the turn names by a distinctive phrase of its own is carried beside
whatever the turn resolved.

#### Scenario: A domain that is an ordinary page is served beside the resolved anchor
- **WHEN** a turn resolves an anchor and also names, by a distinctive phrase of its own,
  a current ordinary page that is none of that anchor's neighbourhood
- **THEN** the packet also carries that page as an anchor of kind `page` at status
  `retrieval_carried`, marked `generation.also_carried = "retrieval"`, with its units;
  the resolved anchor is unchanged and the page is reported `resolved` only when the
  turn also names it by its own title (Hugo's ruling of 2026-10-05, below)
- **AND** a turn that names nothing beyond what it resolved carries nothing extra

#### Scenario: The named anchor keeps its own material ahead of a carried page's
- **WHEN** a turn resolves an anchor and carries a newer page beside it, and both have
  units under the same role
- **THEN** the resolved anchor's units are ranked ahead of the carried page's within the
  role's item cap, and each carried unit's provenance says `carried: true`

#### Scenario: Several pages named apart are each carried
- **WHEN** a turn that resolved no anchor names two pages by two phrases, each phrase
  answering to exactly one page
- **THEN** both pages are carried, bounded at three by score, and a phrase two pages
  answer to is not carried and is never guessed between

### Requirement: Implicit carry respects an explicitly scoped subject request
For bounded unambiguous possessive subject/property constructions, activation SHALL
retain raw clause and naming occurrences and apply conservative subject-scoped
admission to material reached only through body contact in that request. It SHALL
NOT infer the construction merely from nearby object and topic words, and unsupported
syntax SHALL retain existing behavior. Independent page title/alias occurrences and
explicit agent anchors SHALL retain their existing context semantics. Another
occurrence naming the same page SHALL remain an independent admission route.

Property-clause-only body contact SHALL require actual hydrated rich-unit subject
association through `about_entity` and matching authored category identity or an
accepted alias in its existing registry scope before unit projection. This supplies
context association, not certified property/value ownership. Kind headings, tags,
page containment, generic links and nearby prose SHALL NOT supply that proof; several
subject relations SHALL NOT assign all sentences to all subjects. The compiler SHALL
NOT introduce a property-to-category ontology, infer a mapping or alter authored prose.

The same admission predicate SHALL apply to categorical and material unit projection
and uncovered-prose pointer creation. Rejected content SHALL NOT return as a pointer.
Missing proof MAY omit otherwise useful compact legacy context or generic formulas
on this route, but SHALL NOT claim the vault lacks a property. Withheld and absent
subject evidence SHALL remain indistinguishable. Independently admitted standing
personal context, other named domains and body-only carry outside the recognized
request SHALL retain their existing behavior, permissions and bounds.

#### Scenario: A conversion task does not carry unrelated equipment settings
- **WHEN** a scoped request asks to convert one object's target temperature and an
  unrelated page shares conversion words but has no admitted subject association
- **THEN** its saved settings are omitted with semantic evidence both off and on,
  and empty rejected material does not restore a weak ordinary-word band resolution

#### Scenario: Another occurrence independently names the page
- **WHEN** the same turn also explicitly names the conversion page in another
  occurrence, even with words shared by the scoped request
- **THEN** that independent page request retains its context and pointer semantics

#### Scenario: A subject-scoped unit does not license its unrelated sibling
- **WHEN** one rich unit has the admitted subject association and matching authored
  category while a sibling has only another subject or lacks that association
- **THEN** only the associated context is admitted by the property-body-contact route,
  without labeling it as a certified property value

#### Scenario: Uncovered prose cannot bypass the scope restriction
- **WHEN** a rejected page repeats the same task words in uncovered prose
- **THEN** matching that prose alone does not emit a pointer to the page

#### Scenario: Existing independent context remains useful
- **WHEN** a turn combines the scoped request with a separately named domain or
  standing personal context, or another turn uses useful unconstrained body contact
- **THEN** those independent routes retain their existing admission and limits

### Requirement: Title qualification is local to each named occurrence
When the strict two-distinctive-word path names no page, the existing title
fallback SHALL use a complete current title stated in one sentence to qualify
that occurrence only. The occurrence SHALL contain a pair already admitted
under the fallback's word, rarity and proximity rules. A candidate SHALL
support the whole qualifying title phrase, not merely a shared suffix; equal
namesakes and longer titles containing that phrase SHALL remain contested.

Nested matches and loose pairs touching a qualifying occurrence SHALL be
consumed only there. Independently stated full titles and shorter phrases
elsewhere SHALL remain separate domains; non-contained overlapping titles
SHALL remain contested. Unqualified occurrences SHALL retain the existing
partial-title behaviour. Words consumed by a resolved anchor SHALL NOT supply
a new qualifier. Qualification SHALL NOT use a global best-title ranking or
infer uniqueness from incomplete or truncated candidate evidence.

The fallback SHALL retain current maintained-catalogue bounds, page eligibility,
raw-token and sentence semantics, packet budgets, disclosure, continuity and
named-versus-carried statuses. It SHALL NOT add a foreground corpus scan,
index repair, model call or second matching system.

#### Scenario: A full title distinguishes shared-suffix siblings
- **WHEN** the turn states one current page's complete qualifying title and
  other current pages share only its suffix
- **THEN** only the page supporting the whole title qualifies that occurrence

#### Scenario: Separate title occurrences remain separate domains
- **WHEN** the turn states two qualifying titles in disjoint occurrences
- **THEN** each occurrence retains its own candidate group under the existing
  concurrently relevant context rules

#### Scenario: A shorter name stated elsewhere is not consumed
- **WHEN** a longer qualifying title contains a shorter title, and the turn
  also states the shorter phrase independently
- **THEN** the longer occurrence consumes its nested match only, while the
  independent shorter occurrence retains its existing candidate group

#### Scenario: Genuine namesakes do not become a best match
- **WHEN** equal titles or longer titles support the whole qualifying phrase
- **THEN** that occurrence remains contested and no candidate wins by score

#### Scenario: Non-contained overlapping titles remain contested
- **WHEN** qualifying title occurrences overlap and neither contains the other
- **THEN** qualification does not discard either competing interpretation

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
as such, never presented as current. The default applies when the turn's own words
reached the entity; an entity that only recency supplied ("where were we") is read
through its own identity and facets, not through every conclusion linked to it.

#### Scenario: A settled decision about an organisation is served with the entity
- **WHEN** a turn names an organisation entity and a decision note links to that entity
- **THEN** the packet serves the decision unit with `provenance.category: "decision"`
  and its parent page, and serves no decision note that does not link to the entity

### Requirement: A resolved entity's conclusion pages are eligible without word overlap or link-list room
The pages that link to a resolved entity and hold `decision`, `insight` or `finding`
units SHALL be eligible under `precedents` whether or not they share a word with the
turn and whether or not they fit within the entity's capped list of typed links. The
index SHALL record inbound wikilinks past that cap under a relation of their own that
never counts toward the anchor's neighbourhood (resolution reads what it always
read). At most six such pages beyond the neighbourhood are read, chosen newest first
over every holder (never the alphabetically first), and their units are subject to the
lane and packet budgets and to the same egress guard. An anchor that only recency
supplied is skipped, whichever route selected `precedents`.

#### Scenario: A topic-specific conclusion past the link cap is served
- **WHEN** a turn resolves a person entity that more pages link to than the link cap
  keeps, and one of those pages holds a `decision` unit that shares no word with the turn
- **THEN** the packet serves that unit under `precedents`, and serves no linked page
  that holds no conclusion unit and no conclusion page that does not link to the entity

### Requirement: A project's standing precedent page is served for its resolved anchors
A page SHALL declare itself the standing precedent of its project with frontmatter
`standing: true` and the project key in its own `project` or `projects`. When a turn
resolves an anchor in that project (a project anchor, or a page anchor whose own
frontmatter names the project), the compiler SHALL serve that page's units under
`precedents`, at most one page per project and two standing pages per packet, at most
two units per page, each unit no longer than 360 characters (a longer one is a
`unit_too_long` pointer), ahead of the role's other units and outside its item cap,
charged to the packet's character budget. An anchor, or a project, that only recency
supplied ("where were we") is not read for conclusions or a standing page. Several declarations in one project resolve to the most
recently updated. Another project's standing page, and a turn that resolved nothing in
the project, serve none. The reach runs inside the `precedents` lane, behind that lane's
own request-budget gate and under its timing span, so it adds no stage of its own and
is skipped with the lane when the request budget cannot afford it.

#### Scenario: A referent that only recency supplied serves neither
- **WHEN** an entity or a project is the packet's referent through recency alone, and
  `precedents` is selected by another route (a turn cue such as "before")
- **THEN** neither the entity's conclusion pages nor the project's standing page is served

#### Scenario: The methodology page constrains a turn that never names it
- **WHEN** a turn resolves an entity whose project declares a standing page and the
  turn shares no word with that page
- **THEN** the packet serves the page's unit under `precedents`, and serves neither a
  second standing page of that project nor another project's standing page

### Requirement: A served scoped claim keeps its scope qualifier
A unit SHALL be served whole or not at all. The compiler SHALL NOT cut a unit's text
short, because a cut drops a qualifier ("chronic X", "for Y only") and turns a scoped
claim into a general one. A unit longer than 900 characters SHALL be reported as a
pointer with reason `unit_too_long`, never as a half-claim.

#### Scenario: A qualifier past the preferred size survives
- **WHEN** a unit longer than 360 characters ends with its scope qualifier
- **THEN** the packet serves the unit's full text including the qualifier

#### Scenario: A unit too long to serve whole becomes a pointer
- **WHEN** a unit is longer than 900 characters
- **THEN** it is not served as a unit and is reported as a pointer with reason
  `unit_too_long`

### Requirement: A carried page reports the names the turn and its units give it
A page the retrieval carry admits SHALL be reported `resolved`, with evidence
`[lexical_overlap, retrieval]`, when the turn makes name contact with the page's own
title under the anchor rule (two or more shared authored title terms); otherwise it
stays `retrieval_carried` on `retrieval` alone. Admission SHALL NOT change: a phrase
two pages answer to still asks, and only a current page is carried. No evidence kind
or status clause is added for this. Recorded as Hugo's ruling of 2026-10-05.

Where the carried pages are the packet's only anchors, each page that served units
SHALL also list, at most two per page and within the ordinary six-anchor allowance,
the anchor rows wikilinked to or from it whose title or alias a served unit names.
Each SHALL be `partial` on `carried_link`, SHALL name the page in `via`, and SHALL
never resolve, run a lane or be carried forward by a continuity token. A row the reader
may not see SHALL NOT be listed, and the egress guard SHALL remove a listed row
whenever it removes the page named in its `via`.

#### Scenario: A page the turn names by its title is resolved
- **WHEN** a turn names a current ordinary page by two words of its title, such as
  "the kelvane intake checklist", and the carry admits that page alone
- **THEN** the page is `resolved` with `[lexical_overlap, retrieval]` and its units
- **AND** a page reached only through a body phrase, or through one title word, is
  still `retrieval_carried`

#### Scenario: A person the carried unit names is listed beside the page
- **WHEN** a turn that never names a person carries a note whose served unit names that
  person and links their page
- **THEN** the person's anchor is listed `partial` on `carried_link`, never `resolved`
- **AND** a person the note links but the unit does not name, or one withheld from the
  reader, is not listed

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
