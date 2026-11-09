# context-activation Specification

## Purpose
Turn a raw user turn into the minimum sufficient working-memory packet for a fresh agent, without the agent formulating a recall query: a read-only `activate_context` operation resolves anchors from a derived activation index on categorical evidence, abstains when the turn is unresolved or ambiguous, runs bounded role lanes over existing primitives, and serves a budgeted, provenance-bearing packet through the same governance release plane as recall while leaving `ask_memory` and `find` byte-identical.

## Requirements

### Requirement: Read-only activation operation
The product SHALL expose a read-only operation `activate_context` that accepts a raw
user turn (`turn`, non-empty text), an optional `max_chars` (default 4000, clamped
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

### Requirement: Derived activation index
The product SHALL maintain an activation index as a disposable derived sidecar under
the machine-local state root, built only from governed structure: entity pages (title,
`aliases`, entity type, attributes), hub pages outside the immutable raw-material
folders (`Sources/`, `Evidence/`), `Products/` and `Systems/` pages,
active Planning items (title, kind, tags, execution pointer), Records collection
manifests (title, item schema field names, `claims`), and project keys. Each anchor
row SHALL carry a canonical ref, an anchor kind, a lifecycle, a structural signature
extracted without any generative model, alias/lexical terms, an optional signature
embedding produced by the configured embedding backend, typed link summaries and the
index generation. The index SHALL be rebuildable from the vault alone, SHALL be updated
incrementally on the recall freshness checkpoint with a write-generation token, SHALL
never be read by ordinary recall lanes, and SHALL never be treated as canonical truth.

#### Scenario: Rebuild equals incremental
- **WHEN** an index built incrementally across a sequence of vault writes is compared
  with an index rebuilt from scratch at the final vault state
- **THEN** the two contain the same anchor rows, aliases, categories and links, and the
  same signature text

#### Scenario: Missing index is built or reported, never faked
- **WHEN** `activate_context` is called and no index exists for the vault
- **THEN** the unmanaged runtime builds it inline within the request budget, while a
  managed runtime schedules a single-flight background build and returns a packet with
  `abstained: true` and `abstention.reason = "index_warming"` for that request only

#### Scenario: Structured items stay out of recall
- **WHEN** a Planning item or Records item exists in the vault
- **THEN** it may appear as an activation anchor, but the activation index does not
  make it a candidate of `ask_memory`

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
neighbour and neither is a neighbour of the other, where an anchor's anchor
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
`claims_match` SHALL be computed with the existing collection-claims routing and
`graph_corroboration` SHALL count a typed edge between two candidates even when both
already appear in ordinary recall.

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

### Requirement: Bounded role lanes and the working-memory packet
For each resolved anchor the operation SHALL select context roles per the
`context-roles` capability and run one bounded lane per selected role over existing
primitives only: semantic units filtered by category within the anchor neighbourhood,
Records collection items for collections claiming the anchor, active Planning items
linked to the anchor, entity or profile page facets, `graph_context` under a
traversal profile from resolved anchors at depth at most 2 (depth 1 from `partial`
anchors), and evidence pointers. The packet SHALL contain `anchors[]` (ref, title,
kind, status, evidence), `roles[]`, `units[]` (ref, role, text of at most 360
characters cut only at a boundary that leaves no unclosed wikilink, lifecycle,
updated, provenance), `pointers[]` (ref, title, why), `current_state[]` (anchor,
source, as_of, `statement` of at most 200 characters stating the observed status in
the source's own words), `missing[]` (role, reason), `ambiguity[]`,
`budget {limit_chars, used_chars}`, `generation {roles_hash}` (and, for the owner,
`index_generation`, which advances on every write, and `freshness_key`, which counts and
digests every file in the vault; neither is returned to another audience, whose continuity
token carries generation 0) and `abstained`. `used_chars` SHALL count every prose field of the
packet (unit text, `current_state[].statement`, pointer title and why) and SHALL
never exceed `max_chars`. Units SHALL be emitted before pages, pages beyond the
budget SHALL become pointers, and a superseded unit SHALL be marked `superseded`
with its active successor named rather than presented as current. A lane that
reaches its read limit before exhausting the anchor neighbourhood SHALL report
`missing[] {role, reason: "lane_truncated"}`, and a lane whose items fit neither as
units nor as pointers within `max_chars` SHALL report `missing[] {role, reason:
"budget"}`; an item the egress guard removes because it names material released to
the caller at a notice level SHALL be reported as `missing[] {role, reason:
"withheld"}` once per affected section without naming the item, where `role` holds the
packet section name (`anchors`, `units`, `pointers`, `current_state`) rather than a
context role. Material withheld at L0 SHALL be omitted exactly as absent material is:
no marker, and a packet whose every anchor it removed abstains as `unresolved`. The packet SHALL NOT carry the
`due_state` block and SHALL NOT read or advance the due-state emission ledger; recall
remains the only `due_state` carrier.

#### Scenario: Budget holds under a large neighbourhood
- **WHEN** the selected lanes yield more candidate text than `max_chars`
- **THEN** the packet's `used_chars` is at most `max_chars`, units are kept in role
  priority order, and the overflow is represented only as pointers

#### Scenario: Superseded knowledge is marked
- **WHEN** a role lane yields a unit whose page carries `status: superseded` and a
  `superseded_by` target
- **THEN** the unit either is omitted in favour of the successor's unit or appears with
  `lifecycle: superseded` and `provenance.superseded_by` set; it is never emitted as
  active

#### Scenario: Current state comes from Records first
- **WHEN** a resolved resource anchor is claimed by a Records collection whose latest
  item states the resource's status
- **THEN** `current_state[]` carries that status as `statement` with `source: records`
  and the item's `observed_on`, and the `current_state` role lane does not substitute
  an older prose note for it

#### Scenario: A truncated lane is reported, not hidden
- **WHEN** a role lane's read limit is reached while in-neighbourhood units remain
- **THEN** `missing[]` carries `{role, reason: "lane_truncated"}` for that lane

#### Scenario: Activation never consumes the due-state emission
- **WHEN** `activate_context` is called and `ask_memory` is then called on the same
  vault in the same process with a due-state block pending
- **THEN** the `ask_memory` response carries the same `due_state` block it would have
  carried had `activate_context` not been called

#### Scenario: Abstained packet injects nothing
- **WHEN** the turn is `unresolved`
- **THEN** `units`, `pointers` and `current_state` are empty and `budget.used_chars`
  is 0

#### Scenario: L0 material is omitted as absent material is
- **WHEN** the egress guard removes an item that names a page withheld from the
  caller at L0
- **THEN** `missing[]` carries no `withheld` marker for it, and a packet whose every
  anchor was removed abstains with `abstention.reason = "unresolved"`

### Requirement: Governance and egress
Every lane SHALL run under the governance release plane that `find` uses, and every
served packet SHALL be passed through an egress guard that receives the same release
object as hit projection, so that no anchor, unit, pointer, provenance string,
`missing[]` entry or `current_state` entry names a withheld page, including inside
wikilink syntax in `units[].text` and `current_state[].statement`. The guard SHALL
run whether or not the `retrieval` evidence was available: a recall failure degrades
one evidence kind and never bypasses the guard, and a failure of the release plane
itself SHALL abstain rather than serve. The declared `purpose` SHALL be honoured
exactly as it is for recall and SHALL never enter ranking or a cache key.

#### Scenario: Recall failure does not bypass the guard
- **WHEN** an active policy withholds a page from the caller's audience and the
  internal recall call raises
- **THEN** the packet is still guarded and names nothing from the withheld page

#### Scenario: A permitted wikilink in prose survives the guard
- **WHEN** a permitted unit's text contains a wikilink to a page the caller's audience
  may read, or to a page that does not exist
- **THEN** the unit is served unchanged; only a wikilink to a withheld page drops it

#### Scenario: Withheld page never leaks through unit prose
- **WHEN** a permitted unit's text contains a wikilink to a page withheld from the
  caller's audience, spelled by filename stem, by page title, by any frontmatter
  alias of the page, with a display alias (`[[page|label]]`) or with a heading
  (`[[page#section]]`), in any Unicode form of that spelling the page-name map
  resolves
- **THEN** the served packet contains no reference to that page in any unit's text,
  and the match is made on the spelling the prose actually contains, never only on
  a normalised form of it

#### Scenario: An ungoverned vault never depends on the activation sidecar for release
- **WHEN** no governance policy is active, no page is withheld and the packet carries
  prose wikilinks
- **THEN** the guard serves the packet without consulting the page-name map, and a
  sidecar fault cannot abstain the read

#### Scenario: Withheld page never leaks through provenance
- **WHEN** a governed audience withholds a page that is a typed neighbour of a resolved
  anchor
- **THEN** the packet contains no reference to that page in any field, and the anchor's
  `graph_corroboration` evidence, if it depended on that page, is dropped

### Requirement: Freshness and caching
Packets SHALL be keyed on the recall freshness key, the activation index generation,
the role-registry hash and a digest of the retrieval refs the release plane admitted
for the request, so that a packet compiled with one audience's retrieval evidence is
never served to another. When the index generation is behind the freshness
checkpoint, the operation SHALL either refresh the index within the request budget or
mark the packet `generation.index_stale: true`; it SHALL never serve a packet whose
generation block misreports the index generation it was built from.

#### Scenario: One audience's packet is never served to another
- **WHEN** two requests share turn, `max_chars`, freshness key, index generation and
  roles hash but the release plane admitted different retrieval refs for them
- **THEN** the second request is compiled from its own retrieval refs, not served the
  first request's cached packet

#### Scenario: Write invalidates a cached packet
- **WHEN** a packet was served and a governed write then adds an alias to an entity
  named in the next turn
- **THEN** the next call reflects the new alias and reports a newer
  `index_generation`

### Requirement: Instrumentation and latency bound
The operation SHALL record timing spans `working_set.index`, `working_set.resolve`,
`working_set.roles`, `working_set.lanes.<role>` and `working_set.budget` under the
existing timings collector, SHALL satisfy `sum(stages) <= total_ms`, and the CI latency
gate SHALL bound a warm activation on the model-free synthetic corpus by an absolute
ceiling and by a 2,000→8,000-note scaling ratio, both pinned in the gate alongside
the referents ceilings.

#### Scenario: Stages partition the total
- **WHEN** `activate_context` runs with `include_timings=true`
- **THEN** every recorded stage is a registered interval and the stages sum to at most
  the reported total

### Requirement: Surface parity and pins
The tool SHALL be present on the canonical MCP surface, the CLI and the REST facade
with one documented contract; the schema fidelity fixture, both tool-surface digests,
the hosted plugin and candidate trees, `docs/capabilities.md`, the README tool table
and the skill scaffold SHALL be regenerated in the same change, and the scaffold
no-leak gate SHALL pass.

#### Scenario: Schema fixture matches the served surface
- **WHEN** the schema fidelity test runs after this change
- **THEN** the served tool list and the pinned fixture agree and include
  `activate_context`

### Requirement: Activation resolves a turn over the caller's view

For a caller other than the owner under a governed policy, anchor resolution SHALL use
the catalogue a vault without the pages withheld from that caller would hold. The
anchors a turn's words can reach (by name, alias, derived short name or name term)
SHALL be decided before evidence is assembled; name-term counts SHALL count visible
owners only, stopping once a term is common among them; a derived short name retired
only by a withheld anchor SHALL be restored; and an anchor reached only by other
evidence SHALL be decided before resolution. Only those anchors SHALL be decided, so
the request stays within the activation path's filesystem ceilings. Such a caller's
packet SHALL be neither served from nor stored in the packet cache. The owner's
resolution, packets and cache SHALL be unchanged.

#### Scenario: A withheld anchor shares a visible anchor's name
- **WHEN** a withheld anchor shares a visible anchor's title or alias, retires its
  derived short name, makes a turn word common, or is the only anchor a turn names
- **THEN** a restricted caller's packet is byte-identical to the packet from a vault
  without the withheld anchor, and never abstains as `withheld`

### Requirement: A band resolution on an ordinary word yields to a different carried page
A turn's band resolution is yieldable when every anchor the turn resolved carries
`vector_band`, would be at most `partial` without it, and was reached through name
words that are all in a cased script and that the turn never writes with an initial
capital, in a turn that carries a casing signal. A turn carries a casing signal when
it writes at least one cased word with an initial capital and at least one cased word
without one; a sentence-initial capital counts. A name word the turn writes with an
initial capital anywhere, sentence start included, or writes in a script with no
case, names the anchor, and that resolution SHALL NOT yield. A turn with no casing
signal (all lower case, all capitals, or every word capitalised) SHALL NOT yield. For a yieldable
turn that is not referential and names no agent-chosen anchor, the operation SHALL ask
the retrieval carry for the pages the turn names. When the carry names exactly one
dominant page that is not one of the band-resolved pages, and a carried packet can be
built from that page, the operation SHALL serve that carried packet, the one the same
turn gets with semantic evidence off. In every other case — the carry names one of
the band-resolved pages, several pages or none, cannot run, or its page yields no
carried packet — the band's resolution SHALL stand outside the explicitly scoped
subject-request exception below. A turn with any anchor resolved without `vector_band` SHALL NOT ask the
carry for this purpose, and the rule SHALL NOT change which evidence kinds resolve an
anchor outside that exception.

For a bounded possessive subject/property request recognized under the existing
context-activation scope contract, activation SHALL evaluate the affected ordinary-word
band candidate locally. If it lacks independent subject contact and meets the existing
name/casing protections, it SHALL remain at most partial before packet branching,
whether or not rival material survives. An independently resolved anchor elsewhere
SHALL retain its status and SHALL NOT restore that weak candidate. Independently
named pages retain their ordinary admission; property-clause-only body contact must
pass the subject-scoped admission predicate. This exception SHALL NOT widen
retrieval-only identity resolution or veto protected multilingual names.

#### Scenario: A scoped conversion request does not substitute another object's facts
- **WHEN** a turn with a casing signal asks to convert one object's target temperature,
  that object's page has only the yieldable ordinary-word band evidence, and another
  object's settings page shares the conversion words without subject admission
- **THEN** the weak equipment candidate is not `resolved`, the unrelated settings and
  their prose pointers are not served, and both evidence arms preserve honest
  partial/unresolved subject status rather than substituting saved facts

#### Scenario: An independent anchor does not restore a weak property candidate
- **WHEN** that scoped request also independently names another project or page
- **THEN** the independently named context survives while the affected ordinary-word
  band candidate remains at most partial

#### Scenario: A rare name is not vetoed by an incidental phrase
- **WHEN** a German or Russian turn names an anchor by a capitalised rare name
  ("Quillmere", "Pellimore"), the turn clears the band against it, and an unrelated
  ordinary note shares a phrase with the turn that the retrieval carry names
- **THEN** the anchor resolves on `rare_term` + `vector_band`, the carry is not asked,
  and with semantic evidence off the anchor is not `resolved`

#### Scenario: A name in a script with no case is not vetoed
- **WHEN** a Japanese turn names an anchor inside a compound ("白樺小屋") and the turn
  clears the band against it
- **THEN** the anchor resolves on `rare_term` + `vector_band` whatever other page
  shares words with the turn

#### Scenario: An unscoped carried page that yields nothing does not cost the band's page
- **WHEN** a yieldable turn's retrieval carry names one different page, but the carry's
  lanes read nothing off that page and no recognized subject/property request applies
- **THEN** the band-resolved page stays `resolved` and the packet is built from it

#### Scenario: The carry names the band's own page, several pages or none
- **WHEN** a yieldable turn's retrieval carry names the page the band resolved, two or
  more pages, or none, outside the recognized subject/property exception
- **THEN** the band's resolution stands

#### Scenario: An anchor resolved on its own words is not yieldable
- **WHEN** a turn resolves one anchor on `exact_alias` and another on `rare_term` +
  `vector_band`
- **THEN** the carry is not asked for this rule and both anchors keep their status

#### Scenario: Known limit: a turn with no casing signal never yields
- **WHEN** the same unit-conversion turn is typed entirely in lower case ("can you
  convert the grill's target temperature from fahrenheit to celsius?")
- **THEN** the turn carries no casing signal, the band's resolution is not yieldable,
  and the equipment page resolves on `rare_term` + `vector_band` exactly as it did
  before this requirement

### Requirement: A spaced closed compound reads as its closed word
The resolver SHALL join each pair of adjacent non-stopword tokens in one clause into a turn term, unless exactly one of the two tokens is capitalised inside a sentence. A join SHALL never be a turn phrase, so it SHALL never earn `exact_alias` on its own. When a join equals a name term of any indexed anchor the caller can see, whether or not the turn reached that anchor, the resolver SHALL treat the two tokens as that word: neither token SHALL, as a single word, earn `exact_alias`, `lexical_overlap` or `rare_term` for any anchor through that occurrence. A longer phrase that contains the tokens SHALL keep its evidence. A token that the turn also writes outside such a join SHALL keep its own evidence. The same reading SHALL apply to earlier conversation entries. A carried page's title SHALL consume halves against the title's own words. The audience decision for a non-owner reader SHALL read the same turn terms, joins included, as candidate matching, so its name-term counts leave out withheld owners of a join.

#### Scenario: A compound written with a space does not name an anchor keyed on its half
- **WHEN** a turn writes "home work tracker" and the vault holds a project named "Home" and an anchor named "Homework Tracker"
- **THEN** the project is not a candidate and the tracker resolves

#### Scenario: A half written on its own keeps its anchor
- **WHEN** a turn writes "home" on its own and also writes "home work"
- **THEN** the project named "Home" keeps `exact_alias`

#### Scenario: A capitalised name beside a compound half keeps its anchor
- **WHEN** a turn writes "the book Mark lent me" and the vault holds a person named "Mark" and a project named "Bookmark Sync"
- **THEN** the person keeps `exact_alias`

#### Scenario: An earlier entry's compound is read against the whole catalogue
- **WHEN** an earlier user entry writes "home work tracker" and the current turn reaches only the project named "Home"
- **THEN** the entry does not name the project

#### Scenario: A withheld anchor reached only through a join is decided
- **WHEN** a non-owner reader's turn reaches a withheld anchor only through a join
- **THEN** the audience decision withholds that anchor and leaves it out of the join's name-term count

### Requirement: The turn slice follows the conversation
In `maps` mode, the turn slice's anchors compile SHALL receive the request's `conversation` and `trigger`, and SHALL apply the conversation-aware activation contract to them as the `anchors` packet does. It SHALL receive no continuity refs, no `anchor` and no attribution, and SHALL NOT mint a token, learn or record heat. A slice lead that the conversation reached SHALL carry the `conversation` evidence kind, and a lead whose cue the user did not speak SHALL carry the `origin` (`focus` or `conversation`) that the anchors packet gives it. The conversation SHALL NOT change the map. For a restricted caller, the compile SHALL read the conversation's refs and entries over the caller's view. The slice's description SHALL say that the block holds what the turn and its conversation reached, with the evidence, and that it is not checked for relevance.

#### Scenario: A follow-up reaches the subject of an earlier turn
- **WHEN** an earlier user entry names the invented anchor "Pelican Ledger", and the follow-up turn reaches "Pelican Ledger" only by one contact kind that alone gives `partial`
- **THEN** the slice lists "Pelican Ledger" as `resolved`, with the `conversation` evidence kind
- **AND** the same turn without `conversation` lists it as `partial`

#### Scenario: An anaphoric follow-up is carried from the conversation
- **WHEN** the newest earlier user entry resolves "Pelican Ledger", and the follow-up turn points back with no words of its own subject
- **THEN** the slice lists "Pelican Ledger" as `partial` with evidence `[conversation]`
- **AND** the same turn without `conversation` gives a slice without "Pelican Ledger"

#### Scenario: A lead reached by the agent's focus says so
- **WHEN** a turn reaches nothing by its own words, and `conversation.focus` names the invented anchor "Pelican Ledger"
- **THEN** the slice lists "Pelican Ledger" with `origin: "focus"`
- **AND** a lead that the turn's own words reached carries no `origin`

#### Scenario: A conversation that names a withheld page changes no byte for a restricted caller
- **WHEN** a restricted caller's conversation names an anchor and a page that are withheld from it, in a vault and in its twin that lacks them
- **THEN** the two packets that the caller receives are byte-identical

#### Scenario: The slice compile writes nothing
- **WHEN** a `maps` request carries a `conversation` and a continuity token
- **THEN** the slice compile writes no session, heat or learning record; the final maps response may append private composed metadata under served-context-record

### Requirement: A maps packet reports its continuity inputs
In a served `maps` packet, `generation.conversation` SHALL be the state that the turn slice's compile computed for the request's `conversation`. When no slice compile reads the conversation, it SHALL be `absent`. `generation.continuity` SHALL be `absent` when the request carries no token, `applied` when the token carries a map generation and was minted under the serving role registry and conventions, and `stale` for any other token. `generation.continuity_thread` SHALL be `absent`.

#### Scenario: A token reads applied after the map moves
- **WHEN** a caller passes the token of its last `maps` packet after a vault write changed the map generation
- **THEN** `generation.continuity` is `applied`, and the map is sent in full

#### Scenario: A token without a map generation reads stale
- **WHEN** a `maps` request carries a token that cannot be decoded, an `anchors` token with no map generation, or a token minted under other conventions
- **THEN** `generation.continuity` is `stale`

#### Scenario: With the slice off, the conversation is not read
- **WHEN** `EXOMEM_ACTIVATION_TURN_SLICE` is `off` and the request carries a `conversation`
- **THEN** `generation.conversation` is `absent`, and the packet omits the turn slice

### Requirement: Shared index collection preserves caller boundaries
The shared working-set index SHALL collect owner-derived inputs inside the existing internal-producer scope, limited to collection in its write path. The scope SHALL restore the prior principal and disclosure collector on success and failure. It SHALL NOT replace authorization held by an active operation. External reads SHALL retain caller-view filtering, disclosure and historical admission.

#### Scenario: Maintenance collects governed inputs
- **WHEN** an internal index rebuild or update collects governed records
- **THEN** it retains the shared inputs without creating a caller disclosure audit event
- **AND** subsequent external reads still apply caller authorization and emit their required disclosure

#### Scenario: An active restricted operation cannot become the producer
- **WHEN** a restricted caller's active operation reaches shared index collection
- **THEN** the existing authorization mismatch refusal remains authoritative
- **AND** the operation cannot publish an owner-derived sidecar through elevated caller authority

#### Scenario: Collection fails
- **WHEN** shared input collection raises an exception
- **THEN** the prior principal and disclosure collector are restored

### Requirement: Packet modes
Activation SHALL serve one of two packet modes, `anchors` and `maps`, selected by the process setting `EXOMEM_ACTIVATION_PACKET`. `maps` is the default: when the setting is absent or holds another value, activation SHALL serve `maps`. The value `anchors` SHALL restore the `anchors` packet.
- These requirements SHALL bind both modes:
  - "Read-only activation operation", including the kill switch, which disables both modes;
  - "Derived activation index";
  - "Governance and egress";
  - "Freshness and caching";
  - "Instrumentation and latency bound";
  - "Surface parity and pins";
  - "Activation resolves a turn over the caller's view".
- These requirements SHALL bind `anchors` mode only:
  - "Categorical anchor evidence and resolution";
  - "A band resolution on an ordinary word yields to a different carried page";
  - the packet shape of "Bounded role lanes and the working-memory packet".
- The two resolution requirements above ("Categorical anchor evidence and resolution" and the band requirement) SHALL also bind the turn slice, because its leads are what the anchors compile selects.
- The role lanes' bounds and lifecycle rules SHALL also bind profiles that are compiled through those lanes.
- The `continuity` and `conversation` contracts of the conversation-aware activation and continuity capabilities, and the retrieve-inject-hook rendering contract, SHALL bind `anchors` mode. `maps` mode SHALL follow "The maps packet". The `conversation` contract SHALL also bind the turn slice, as "The turn slice follows the conversation" states.

#### Scenario: The default mode is maps
- **WHEN** `EXOMEM_ACTIVATION_PACKET` is absent, or holds a value other than `anchors` or `maps`
- **THEN** activation serves the `maps` packet

#### Scenario: The setting restores the anchors packet
- **WHEN** `EXOMEM_ACTIVATION_PACKET` is `anchors`
- **THEN** activation serves the `anchors` packet and no map

#### Scenario: The kill switch disables both modes
- **WHEN** the working-set kill switch is set and `EXOMEM_ACTIVATION_PACKET` is `maps`
- **THEN** activation returns the disabled abstention and no map

### Requirement: The maps packet
In `maps` mode, activation SHALL return the map, its generation, the budget report, and the same governance and generation metadata as an `anchors` packet. It SHALL NOT call a model other than retrieval scorers, or write to the vault. It SHALL NOT resolve anchors, except for the turn slice, which the setting `EXOMEM_ACTIVATION_TURN_SLICE` turns off and which "The turn slice" governs. The map, any attached profile and the turn slice SHALL share the request's `max_chars` cap. The cap SHALL charge the packet's prose: the map's description line, one rendered line for each map and slice item, and the overflow line. It SHALL NOT charge structure fields: refs given as fields, paths, profile routes, topics, dates, the generation blocks, the continuity token and timings. The budget report SHALL carry `serialized_chars`, the length of the packet's compact JSON form without that field. A map item SHALL omit `topics` when it is empty and `lifecycle_class` and `state` when they have no value. The map's description SHALL name the read call for a map ref. When the owner's map inputs changed after an earlier map for the same owner was built, activation MAY serve that earlier map marked `stale: true`, with `generation.index_stale: true` and the index generation it was built from, and SHALL then rebuild the map off the request thread. A restricted caller's map SHALL NOT be served from an earlier build. A vault with no context-catalogue entries visible to the caller SHALL return an empty map with the reason `empty`. The `continuity` token SHALL carry the map generation, so that a caller can tell when its map is outdated, and SHALL NOT encode anchors. When a request carries a `continuity` token from a `maps` packet whose map generation equals the generation of the map that activation would serve to that caller, the map block SHALL carry its ref, generation, stale flag, `unchanged: true` and a one-line description, and no items. `conversation`, `focus` and `trigger` SHALL NOT change the map, and neither SHALL the turn: the turn, its conversation and its trigger change only the turn slice. A `maps` packet SHALL carry the `episode_due` advisory under the same conditions as an `anchors` packet. The call ledger SHALL record the released-item count of a `maps` packet as unknown, not zero, because a map response carries no per-page basis to count.

#### Scenario: An empty vault returns an empty map
- **WHEN** no context-catalogue entry is visible to the caller
- **THEN** the packet carries an empty map with the reason `empty`

#### Scenario: The map fits the budget
- **WHEN** the caller sets `max_chars`
- **THEN** the map and any attached profile together fit within it

#### Scenario: The budget charges prose and reports the serialized size
- **WHEN** activation serves a `maps` packet for `max_chars` 8,000
- **THEN** `budget.used_chars` is at most 8,000 and counts only the prose lines
- **AND** `budget.serialized_chars` equals the length of the packet's compact JSON form without that field

#### Scenario: An unchanged map is not sent again
- **WHEN** a caller passes the `continuity` token of its last `maps` packet and the map it would be served has the same generation
- **THEN** the map block carries `unchanged: true` and no items
- **AND** a call with no token, or with a token of an older generation, receives the full map

#### Scenario: A write does not hold the owner's activation
- **WHEN** the owner's map was built and a vault write then changes its inputs
- **THEN** the next activation serves the earlier map with `stale: true`, `generation.index_stale: true` and the index generation it was built from
- **AND** after the rebuild finishes, an activation serves the new map with `stale: false`

#### Scenario: Conversation does not change the map
- **WHEN** two `maps`-mode calls differ only in `conversation`
- **THEN** both return the same map

#### Scenario: With the setting off, the packet is unchanged
- **WHEN** `EXOMEM_ACTIVATION_TURN_SLICE` is `off`
- **THEN** the `maps` packet carries no `turn_slice` block and is the packet that the packet mode alone gives for the same vault and turn

#### Scenario: The episode advisory rides on a map
- **WHEN** the conditions of the `episode_due` advisory hold and activation serves the `maps` packet
- **THEN** the packet carries `episode_due`
- **AND** nothing else in the packet differs from the same call without the advisory

#### Scenario: The call ledger does not report a map as zero pages
- **WHEN** activation serves the `maps` packet
- **THEN** the call ledger records the released-item count as unknown, not zero

### Requirement: The turn slice
A `maps` packet SHALL carry a `turn_slice` block beside the map, unless `EXOMEM_ACTIVATION_TURN_SLICE` is `off`. The slice is on by default and for any other value of the setting, and the setting has no effect in `anchors` mode.
- The slice SHALL list the leads that the guarded anchors compile selects for the user turn and the request's conversation: resolved and partial anchors, the senses of an ambiguous turn, pages that the turn names by title, related pages and the carried page. It SHALL NOT list `recent_context`. The anchors compile SHALL be one helper that both packet modes call. In `maps` mode it SHALL run with no continuity refs, no `anchor` and no attribution, SHALL read the conversation as "The turn slice follows the conversation" states, and SHALL NOT mint a token, learn or record heat. It MAY skip the material of the anchors that it resolved, which the slice never serves, and SHALL then give the same leads and state as the full compile.
- The block SHALL carry `state`, one of `ready`, `empty`, `skipped` or `unavailable`, a generation that serves only as the guard's record key, a fixed description, the items and, when some do not fit, an overflow count. The description, which only a `ready` block carries, SHALL say that the block holds what the turn and its conversation reached, with the evidence, and that it is not checked for relevance.
- Each item SHALL carry its catalogue entry's kind, title, description and description origin, its ref, the profile ref and the state line for anchors and collections, `match` and `evidence`. `match` SHALL be one of `resolved`, `partial`, `ambiguous`, `named`, `related` or `carried`. `evidence` SHALL be the categorical evidence kinds that the evidence requirement fixes, with no score.
- Display text SHALL come only from catalogue entries that the caller may see. A lead with no such entry SHALL be dropped. A turn with no admitted lead SHALL give `state: empty` and no item.
- The map SHALL compile first, over the caller's view, at the request's `max_chars` less a reserve that one named constant fixes, or with no reserve where that share cannot hold one line. A slice that fails or finds no room SHALL set its `state` and SHALL NOT remove or change the map. The slice SHALL use a cooperative deadline one activation stage reserve before the request deadline. The final disclosure guard SHALL still check the built map if a synchronous slice stage overruns that deadline. The slice SHALL NOT change `map_generation` or the `continuity` token. Slice lines SHALL be whole lines, lines that do not fit SHALL become the overflow count, and `used_chars` SHALL count the map and the slice together.
- For a restricted caller, the slice's lexical pass and its dependence check SHALL choose the turn's terms and rank on the pages that the caller admits.
- The terminal guard SHALL serve a slice item only from a record that the invocation pinned for the same caller and packet limit, and SHALL serve an `unavailable` slice otherwise. It SHALL drop a slice that stands beside no map.
- The relevant-due-refs association SHALL read the resolved items of the slice as it reads resolved anchors.
- The retrieve hook SHALL render the slice on a turn whose map generation is unchanged.

#### Scenario: An irrelevant turn gets no slice items
- **WHEN** the turn reaches no lead that the guard admits
- **THEN** `turn_slice.state` is `empty` and the block holds no item

#### Scenario: The slice does not change the map
- **WHEN** two turns, one that resolves an anchor and one that reaches nothing, activate over the same vault
- **THEN** both packets carry the same map block, the same `map_generation` and the same `continuity` token

#### Scenario: A withheld anchor changes no byte for a restricted caller
- **WHEN** a vault holds, beside a visible anchor, a withheld anchor of the same name and a withheld titled page
- **THEN** a restricted caller's packet is byte-identical to the packet from a vault without them

#### Scenario: Withheld pages move no lexical statistic of the slice
- **WHEN** a vault holds withheld pages that share words with the turn
- **THEN** a restricted caller's packet is byte-identical to the packet from a vault without them

#### Scenario: Due advice rides on a resolved lead
- **WHEN** the turn resolves an anchor and a due item bears on that anchor
- **THEN** the packet carries `relevant_due_refs` for it

#### Scenario: A failed slice keeps the map
- **WHEN** the slice cannot be built, or cannot be served
- **THEN** `turn_slice.state` is `unavailable` or `skipped`, and the map block is the one that any other turn gets

#### Scenario: A forged slice is not served
- **WHEN** a slice item was not pinned for this caller and packet limit, or the slice was built for a larger packet
- **THEN** the packet carries an `unavailable` slice and the map is unchanged

### Requirement: A context catalogue with bounded projections
The compiler SHALL maintain a context catalogue, distinct from the anchor catalogue. Its entry kinds SHALL be the closed set that its schema declares: topic, anchor, note, thread and collection.
- A note SHALL be a page that is not an anchor, a thread or a collection, and is outside the trees that the product's layout declaration names as raw material (Sources and Evidence), schema, governance or archive. Activation and the catalogue SHALL read that one declaration. A page whose lifecycle class is `retired` stays a note.
- A thread SHALL be the newest episode revision for one episode identity: the `ep-` key of the episode model together with the audience it was recorded under.
- A collection SHALL be a Records or Planning collection.
- Each entry SHALL carry:
  - a one-line description, or none, and the origin of that description;
  - its topic memberships;
  - its source refs;
  - the generation it was built from.

The context catalogue SHALL be a derived sidecar of its own: disposable, machine-local, rebuildable from the vault alone, and never read by recall. An entry's description SHALL be the first of these that exists, and its origin SHALL be the name in front of it:
1. `authored`: the page's `summary` field, or for a collection the `summary` field of its `_collection.md` page;
2. `registry`: for a topic, the `description` field of its domain's registry entry;
3. `model`: a model-made description of the page's current content, when a derived-description store exists;
4. `lede`: the page's structural lede, cut to one line;
5. `none`: the entry is shown as undescribed.

The server SHALL NOT call a model to make a description. When a derived-description store exists, it SHALL be a reserved internal-state path, so that no surface that lists, searches or reads vault files serves it directly. A derived description SHALL be visible only to callers who can see the page it describes.

The map SHALL be a projection of the catalogue that does not depend on the turn. It SHALL fit within the request's `max_chars` cap. Which entries the map lists SHALL be decided by registry-declared lifecycle classes and by entry activity, not by lists in code:
- An entry's lifecycle class SHALL be the status registry's `class` attribute of the entry's status.
- An entry's activity SHALL be its page's `updated` field or, for a thread, the date of its newest revision that the caller can see.
- The map SHALL order entries totally: by these rules, then by activity, newest first, then by ref. `exomem://map` SHALL page through the whole ordered projection, and activation SHALL return its first part within the budget.
- Entries whose lifecycle class is `superseded`, `retired` or `abandoned` SHALL appear only in domain maps. Each topic SHALL have a domain map: a projection of its entries, paginated at the read contract's page size. Entries with no topic SHALL be listed in an unfiled domain map. The map SHALL describe itself as an index of what exists, not as material relevant to the turn.

#### Scenario: The map does not depend on the turn
- **WHEN** two turns activate in `maps` mode with no catalogue change between them
- **THEN** both packets carry the same map

#### Scenario: Cold material stays reachable
- **WHEN** an entry does not fit the map
- **THEN** the domain map of each of its topics, or the unfiled domain map, lists it

### Requirement: Topic membership is declared
A topic SHALL be a domain that the vocabulary registry declares. An entry SHALL belong to a topic only through one of these:
- its page's `topics` field, a value or a list, names the domain;
- one of its page's project keys (its `project` and `projects` fields) maps to the domain through that key's hand-written `domain` field or a `project` carrier in the `topic-carriers` overlay registry; a project anchor belongs to its own key's domains;
- a `folder` carrier declares the domain and its folder contains the entry;
- a `tag` carrier declares the domain and matches one of the entry's frontmatter tags;
- a home declares the domain and its folder contains the entry;
- an authored relation from the entry to a page is of a relation kind whose registry family is `membership`, and that page declares the domain through its own topics, project keys, carriers or home.

A declared value SHALL resolve when the domains resolver returns it as a built-in, registered or alias key; a deprecated key SHALL resolve to its replacement. A value that does not resolve SHALL give no membership, and the catalogue SHALL count it as an unresolved declaration. A page's `domain` field SHALL NOT declare membership, because it names a source's subject or an experiment's folder. Membership SHALL run from the entry to the topic and SHALL NOT be transitive: an entry SHALL NOT join a topic that its relation's target reaches only through its own relations. A mention of an anchor SHALL NOT give the mentioning page that anchor's topics. Notes that are not anchors SHALL be catalogue entries in the topics they declare.

A caller denied the `topic-carriers` or `homes` overlay SHALL receive no membership from that overlay. A relation SHALL add membership only from a target admitted to the caller. Membership SHALL use the current registries at read time, so a carrier save changes the next map without a page write or catalogue refold.

#### Scenario: A vault-added domain gets a domain map
- **WHEN** the vault overlay adds a domain and pages declare it
- **THEN** the context catalogue holds a topic and a domain map for it without a code change

#### Scenario: A project key gives its pages a topic
- **WHEN** the project-key registry maps a key to a domain and a note names that key in its `project` field
- **THEN** the note is an entry of that domain's topic

#### Scenario: An unresolved topic is counted, not guessed
- **WHEN** a page edited outside Exomem names a value in its `topics` field that the domains registry does not hold
- **THEN** the page joins no topic through that value and the catalogue counts one unresolved declaration

#### Scenario: An experiment's folder domain is not a topic
- **WHEN** an experiment page carries `domain: ops` and no `topics` field
- **THEN** it joins no topic through its `domain` field

#### Scenario: A mention does not spread membership
- **WHEN** a note mentions an anchor of another topic and has no relation of the membership family to it
- **THEN** the note does not join that topic

#### Scenario: A project carrier gives its pages a topic
- **WHEN** a project carrier declares a topic and a note names its key in `project` or `projects`
- **THEN** the note joins that topic without a change to the project-key file

#### Scenario: A folder carrier gives contained entries a topic
- **WHEN** a folder carrier declares a topic and its folder contains a catalogue entry
- **THEN** the entry joins that topic

#### Scenario: A tag carrier files a note in a type folder
- **WHEN** a note in a type folder has a tag whose carrier declares a topic
- **THEN** the note joins that topic without a page write

#### Scenario: A home gives membership
- **WHEN** a home declares a topic and its folder contains a catalogue entry
- **THEN** the entry joins that topic

#### Scenario: A denied overlay gives no membership
- **WHEN** a caller may not read the carrier or home overlay that declares a topic
- **THEN** that overlay gives no topic membership to the caller's entries

#### Scenario: A membership target declares topics through a carrier
- **WHEN** an admitted membership-relation target declares a topic through a carrier
- **THEN** the source entry joins that topic without following the target's relations

### Requirement: Profiles read by ref
A caller SHALL be able to read an anchor profile and a topic profile by ref. A profile SHALL be compiled through the role lanes over the anchor or over the topic's members, with the categories and roles that the registries declare. Each profile line SHALL be one of these:
- a semantic unit with its category, role, currency and ref;
- a current-state line from the current-state authority, with its as-of date and the source of that date;
- a ref to a page or a unit.

A unit that does not fit SHALL become a ref and SHALL NOT be cut. A unit's as-of date SHALL be its own authored date. A page's modification date SHALL NOT stand in for a unit's date.

#### Scenario: A superseded unit is marked
- **WHEN** a profile unit has a current successor visible to the caller
- **THEN** the profile shows the successor, or marks the unit superseded and names its successor

#### Scenario: A unit without a date stays undated
- **WHEN** a unit has no authored date
- **THEN** its profile line carries no as-of date

### Requirement: Derived views resolve through one read contract
`read_memory` SHALL resolve these refs on every surface that serves `read_memory`:
- `exomem://map`, the map;
- `exomem://map/topic/<domain key>` and `exomem://map/unfiled`, the domain maps;
- `exomem://profile/topic/<domain key>` and `exomem://profile/anchor/<percent-encoded anchor ref>`, the profiles.

Each response SHALL carry the ref, the view's generation, a stale flag and at most 40 items. Further items SHALL be reachable by an opaque continuation token that is bound to the view, the caller's view and the generation. A malformed, unknown, expired or differently bound token SHALL return the existing `INVALID_CONTINUATION` refusal. A token bound to the same view and caller but an older generation SHALL return `CONTINUATION_STALE`, and the caller restarts from the first page. A ref that names no view visible to the caller SHALL return `NOT_FOUND`. A view SHALL be built from the indexes as they are published. When an input that the caller can see changed and the indexes have not yet published that change, the response SHALL serve the view built from the published indexes, marked stale. A cached build SHALL be served only while its inputs are unchanged, or marked stale; a caller whose builds are not cached SHALL get a build per request. A view is stale only when an input that the caller can see changed. When the catalogue cannot yet tell whether a pending change touches an input that a restricted caller can see, it SHALL answer that caller with the warming state, not with a stale mark. A view's generation SHALL be a digest of what the caller can see, so two generations compare only as equal or different. Titles, descriptions, current-state statements and every profile line SHALL be served without wikilinks, and the terminal guard SHALL serve each item's title and description from the catalogue entry that the caller may see, so that display text never depends on a page the caller cannot see. Each item SHALL carry the display text that its generation was compiled from: the terminal guard rechecks authorization against the current state, and it SHALL drop an item rather than serve newer text under an unchanged generation. Topic membership, topic items and the domain registry's labels and descriptions SHALL be served only to a caller whom that registry's admission contract admits. For any other caller, a topic ref SHALL return `NOT_FOUND` with no warming state, and no item SHALL be filed under a topic.

#### Scenario: A stale profile is marked
- **WHEN** a member of a profile changed and the indexes the profile reads have not yet published that change
- **THEN** reading the profile returns the build from the published indexes, marked stale, for the owner and for a restricted caller alike

### Requirement: Descriptions and projections stay current on write
A write tool that creates or changes a context-catalogue entry SHALL accept a one-line description and a `topics` value from the writing agent, and store them in the page's `summary` and `topics` fields. A description that the agent did not supply SHALL be derived and marked as derived. A stored derived description SHALL NOT be stored under Sources or Evidence. A write that sets a page's `topics` field SHALL resolve each value through the domains resolver that source capture uses, and SHALL register a new value as capture does, with a warning on a near-miss. When registration refuses a value, as capture does on an invalid key or a path collision, the write SHALL refuse with the same error. A write SHALL invalidate every projection and profile whose inputs it changed, including old and new memberships and incoming relations. Publishing a projection SHALL mean replacing the current one. A build from a catalogue state older than the current one SHALL NOT be published.

#### Scenario: A capture reaches the profile it touches
- **WHEN** an agent records a new decision about an anchor and derived sync reports the profile refresh complete
- **THEN** the anchor's profile carries the decision with its own date

#### Scenario: A rename moves the entry
- **WHEN** a page is renamed and derived sync reports the refresh complete
- **THEN** the context catalogue, the map and every domain map that listed it carry the new ref and no line for the old one

### Requirement: Projections are computed over the caller's view
The context catalogue, map, domain maps and profiles served to a caller SHALL be computed over that caller's view. A hidden entry SHALL NOT affect anything served from them:
- any description;
- selection, ranking or ordering;
- caps or counts;
- staleness marks;
- generation tokens.

The egress guard SHALL cover every new packet block and field.

#### Scenario: A withheld entry changes nothing a non-owner sees
- **WHEN** a withheld entry is added to a topic that a non-owner can see
- **THEN** that caller's map, domain map and topic profile are unchanged, including their order and counts

### Requirement: The default packet mode follows an operator ruling
The default packet mode SHALL be `maps` by operator ruling, not by the result of an evaluation. A paired evaluation SHALL keep measuring first answers and finding gaps in `maps` mode, and it SHALL NOT gate the default. Its held-out split SHALL stay sealed and unread until one later measure, which is not a switch gate. These parts of the evaluation SHALL be fixed before any held-out generation:
- the cases, their groups and the arms, with each arm's server mode, tools and instructions;
- the agent and its pull budget;
- the scoring rule and the judge's verdict schema;
- the retry and failure policy;
- the tolerances and the decision rule.

The arms SHALL include a pull-instructions arm with no map. The cases SHALL include ordinary turns sampled without regard to any arm's outcome, beside the cases of known misses. The primary measure SHALL be whether the answer to each case's scored turn uses the required current facts and constraints and asserts no forbidden claim as current. The report SHALL state whether each of these holds:
- on held-out cases that expect memory, miss cases and ordinary turns pooled, `maps` is superior to `anchors` on the primary measure, with each stratum also reported;
- on held-out ordinary turns, `maps` is non-inferior to `anchors` within the fixed tolerance;
- `maps` is superior to the pull-instructions arm, or non-inferior to it within the fixed tolerance and superior on memory calls per case;
- contamination on cases without relevant memory is non-inferior within the fixed tolerance;
- `maps` loses no activation-audit positive case that `anchors` passes.

#### Scenario: The evaluation does not gate the default
- **WHEN** the held-out evaluation has not run, or has run and not met every comparison
- **THEN** activation still serves the `maps` packet by default

### Requirement: Activation carries the live capture policy
When the request carries no continuity token, or its token names no capture-policy digest or a digest that differs from the current one, `activate_context` SHALL attach one `capture_policy` block. The block SHALL state the effective `proactive_capture` disposition in plain words and, unless that disposition is `off`, the vault's effective `link_acceptance` disposition, the durable-capture classes with their routing and quiet negatives, the check of the relation queue before an affiliation facet, the correction rule (stored advice the user says failed, or a claim the user corrects, is durable and is corrected under `proactive_capture`), the hand-over rule under `proactive_capture` (keep the exact bytes of every generated file handed to the user), and the route for a reported delivery (search for the artifact's Evidence receipt, then append one delivery Record that links it). The server SHALL build the block from the constants that bootstrap serves, attach it outside the packet cache, reserve its characters inside `limit_chars` from the map's room and not from the turn slice's share, and compute it independently of the turn's content and free of vault content. The server SHALL name the current digest in the continuity token that the response returns when the caller then holds the current policy, because the response carries the block or the request's token already named the digest, and SHALL keep no delivery state of its own. A token that is not valid or a digest field that is absent SHALL read as no digest. The block SHALL NOT exceed 1,320 bytes rendered, header included.

#### Scenario: The block agrees with the bootstrap envelope
- **WHEN** the effective disposition is `off`, `advisory` or `silent`, set by prominence, by an envelope override or by the unreadable-preference floor
- **THEN** the block's `disposition` equals the disposition that bootstrap serves for `proactive_capture` in the same scope

#### Scenario: The block's link disposition equals bootstrap's
- **WHEN** the effective `link_acceptance` is `off`, `advisory` or `confirm-shortcut`, set by derivation from the level or by an envelope override
- **THEN** the block's `link_acceptance` equals the disposition that bootstrap serves for `link_acceptance` in the same scope
- **AND** the block's text states that value, not the derived one

#### Scenario: A link_acceptance change re-arms the block
- **WHEN** a caller holds the block and then the vault's `link_acceptance` changes
- **THEN** the next activation packet carries a block with the new value, even when the request carries the continuity token

#### Scenario: The block is sent once and re-armed
- **WHEN** a caller has received the block and next sends the continuity token that the response returned
- **THEN** the packet carries no block
- **AND** a request without a continuity token carries the block again

#### Scenario: The block is sent once over the stateless transport
- **WHEN** a client calls `recall_context` through the stateless HTTP transport, where every request is a fresh session, and echoes the returned continuity token on each call
- **THEN** only the first packet carries the block

#### Scenario: A policy change reaches the next turn
- **WHEN** a caller saves a new preference after receiving the block
- **THEN** the next activation packet carries a block with the new disposition, even when the request carries the continuity token

#### Scenario: A full map still delivers the block
- **WHEN** the map fills the character budget and the block is owed
- **THEN** the packet carries the block and `used_chars` does not exceed `limit_chars`

#### Scenario: The block does not shrink the turn slice
- **WHEN** a request is owed the block and the turn reaches more leads than fit the slice's share of a budget that has no block
- **THEN** the packet serves the same leads as the same request that holds the policy
- **AND** the map's index is the part that holds fewer items

#### Scenario: A request too small for the block leaves it owed
- **WHEN** a request's budget would leave the map under the smallest budget that a request may ask for
- **THEN** the packet carries no block and the returned token names no digest
- **AND** the next request that echoes that token and has the room carries the block

#### Scenario: A packet with no token carries the block every time
- **WHEN** the packet carries no continuity token, as an abstention under `EXOMEM_DISABLE_WORKING_SET` does
- **THEN** the packet carries the block, and so does every later packet that carries no token

#### Scenario: The quiet disposition carries no doctrine
- **WHEN** the effective disposition is `off`
- **THEN** the block states that writes need an explicit request and carries no capture classes and no `link_acceptance` value

#### Scenario: The block carries the correction rule
- **WHEN** the effective disposition is `advisory` or `silent`
- **THEN** the block states that stored advice the user says failed, or a claim the user corrects, is corrected under `proactive_capture`
- **AND** the sentence is the one that the `balanced` and `maximal` contracts serve
