## MODIFIED Requirements

### Requirement: Categorical anchor evidence and resolution
Anchor candidates SHALL carry only categorical evidence kinds. Contact kinds establish
that the turn reached the anchor and form two families: worded contact —
`exact_alias`, `lexical_overlap`, `claims_match` and the weak kind `rare_term` — where
the turn's own words reach the anchor's own names, terms or claims; and retrieved
contact — `retrieval` and `vector_band` — where a ranking engine surfaced the anchor. Qualifier kinds —
`category_match`, `graph_corroboration` and `usage_prior` — strengthen an anchor the
turn already reached and SHALL never create a candidate on their own. No float score
SHALL appear in the packet. An anchor SHALL resolve as `resolved` when it carries
`exact_alias`; or when it carries `lexical_overlap` or `claims_match` together with at
least one other kind besides `usage_prior`; or when it carries `rare_term` together with
at least one other contact kind. An anchor whose contact is retrieved only SHALL be at
most `partial`, however many retrieved kinds and qualifiers it carries, and so SHALL an
anchor whose only contact is `rare_term`, whatever qualifiers accompany it; an anchor with
exactly one kind other than `usage_prior`, that kind not being `exact_alias`, SHALL be
`partial`. `retrieval` SHALL be
granted only when the anchor's own page is among the recall hits for the turn in its
own right; a page that recall admitted only by graph expansion from another hit, and a
recall hit elsewhere in the anchor's link neighbourhood, SHALL NOT be contact. A
project-key anchor has no page and SHALL never carry `retrieval`.
`graph_corroboration` SHALL be granted to a candidate only when it is typed-linked to
another candidate that carries a worded contact, and SHALL count such an edge even when
both candidates already appear in ordinary recall. The turn SHALL be
`ambiguous` when two or more resolved anchors of the same anchor kind share no anchor
neighbour and neither is a neighbour of the other, where an anchor's anchor
neighbourhood is the set of its typed-link neighbours that are themselves anchors in
the activation index (resolved anchors of different kinds are complementary; a shared
page that is not an anchor, reached by alias or otherwise, never makes two anchors
complementary; a direct typed link between the two always does; project-key anchors
have no page, so they can neither bridge two anchors nor be anyone's neighbour, and two
resolved project anchors are therefore trivially competing); otherwise,
when no anchor is `resolved`, the turn SHALL be `unresolved` and the operation SHALL
abstain with an empty packet that still lists its `partial` candidates and their
evidence. Lexical comparison SHALL ignore stopwords and SHALL compare terms with
regular plural and singular forms folded together (`-s`; `-es` when the word ends `-ses`, `-xes`, `-zes`, `-ches` or `-shes`;
`-ies` to `-y`), never folding a word of three characters or fewer and never a word
ending in `ss`, `us` or `is`. The fold is a suffix rule, not a dictionary: plurals whose
singular ends in `-se` ("releases", "cases", "uses", "databases") and Greek plurals in
`-es` of singulars in `-is` ("analyses", "bases" of "basis") do not meet their singulars,
and both residual classes SHALL be documented beside the rule rather than patched with
word lists. `lexical_overlap` SHALL be granted
for two or more shared terms of which at least one is among the anchor's own authored
title and alias terms; terms an anchor carries only through tags or section headings MAY
complete an overlap and SHALL never constitute one. `rare_term` and `lexical_overlap`
SHALL be mutually exclusive on one anchor, since both are read from the same
intersection of the turn's words with the anchor's: `rare_term` SHALL be granted only
when `lexical_overlap` is not. `rare_term` SHALL be granted for exactly one term shared
between the turn and the anchor's own authored title and alias terms (never a term the
anchor carries only through a tag, a section heading or a derived name) when that term
occurs among the authored title and alias terms of no more anchors held in the
activation index than the effective rare-term threshold (shipped default three); the
sharing and the count SHALL use one vocabulary, the count SHALL cover every anchor kind
and only anchors actually held in the index, SHALL be measured when the index is built
and SHALL never leave the server. Turn tokens SHALL keep their order and
repetitions for n-gram construction so that two anchors sharing a word in their names
can both receive `exact_alias` from one turn. The index SHALL derive one short name
from an anchor title that carries a trailing parenthetical or dash qualifier and SHALL
treat it as an alias only while no other anchor's names include it and every term of it
is within the rare-term threshold, measured before derived names are added and over
every anchor kind. A derived name SHALL be the title's leading words as the resolver
tokenises them, joined by single spaces; none SHALL be derived when that leaves nothing,
more than three words, a word with fewer than two letters, only stopwords, only digits,
fewer than three characters, or a filename-like lead. `usage_prior` SHALL
only break ties between otherwise equal candidates and SHALL never contribute to the
two-kinds rule. `claims_match` SHALL be computed with the existing collection-claims
routing.

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

#### Scenario: Retrieved evidence alone never resolves
- **WHEN** an anchor's own page is a recall hit, its signature falls in the vector band,
  it is typed-linked to another candidate and a turn cue matches its categories, but no
  word of the turn reaches its names, terms or claims
- **THEN** the anchor is `partial` and nothing is served for it

#### Scenario: A recall hit near an anchor is not contact
- **WHEN** a page linked from a hub is among the recall hits for a turn and the hub's
  own page is not
- **THEN** the hub is not a candidate on that account

#### Scenario: Corroboration needs an independently reached partner
- **WHEN** two typed-linked anchors are both candidates through retrieved contact only
- **THEN** neither carries `graph_corroboration`

#### Scenario: A dense vault does not activate on an unrelated turn
- **WHEN** a turn about nothing in the vault is activated against a vault whose recall
  hits for it fall inside a densely interlinked cluster of anchors
- **THEN** no anchor is `resolved` and the packet abstains as `unresolved`

#### Scenario: One rare word reaches an anchor weakly
- **WHEN** a turn shares exactly one term with an anchor and that term names at most
  three anchors in the index
- **THEN** the anchor carries `rare_term` and, with no other contact kind, is `partial`

#### Scenario: A rare word and a turn cue are not enough
- **WHEN** an anchor carries `rare_term` and `category_match` and nothing else
- **THEN** it is `partial`

#### Scenario: A rare word and the anchor's own page in recall resolve
- **WHEN** an anchor carries `rare_term` and its own page is among the recall hits
- **THEN** it resolves with evidence `[rare_term, retrieval]`

#### Scenario: One common word is no contact
- **WHEN** a turn shares exactly one term with an anchor and that term names more than
  three anchors in the index
- **THEN** the anchor carries neither `rare_term` nor `lexical_overlap` on that account

#### Scenario: Plural and singular agree
- **WHEN** a turn says "posts" and an anchor's terms include "post" along with a second
  shared term
- **THEN** the anchor carries `lexical_overlap`

#### Scenario: A title's leading name resolves while it is unique
- **WHEN** one anchor is titled with a name followed by a parenthetical qualifier, no
  other anchor's names include that leading name, and a turn uses it
- **THEN** the anchor carries `exact_alias`; and when a second anchor with the same
  leading name is added, neither carries `exact_alias` from the short name

#### Scenario: An unresolved turn hands its candidates to the agent
- **WHEN** a turn reaches anchors by retrieved contact only
- **THEN** the packet abstains as `unresolved`, serves no units or pointers, and lists
  those anchors as `partial` with their evidence kinds

#### Scenario: A shared topic prefix is nobody's name
- **WHEN** one anchor is titled with a leading word, a dash and a subtopic, and that
  leading word occurs among the title and alias terms of more anchors than the
  rare-term threshold
- **THEN** no short name is derived for it and a turn using the word does not give it
  `exact_alias`

#### Scenario: A tag is not a name
- **WHEN** every hub anchor carries the tag `hub`, exactly one anchor's title contains
  the word "hub", and a turn says "hub" and nothing else in common with a second hub
  whose own page is a recall hit
- **THEN** the second hub does not carry `rare_term`

#### Scenario: A page recall reached only through a link is not contact
- **WHEN** a note matching the turn links to a hub, recall returns the hub only by graph
  expansion from that note, and no word of the turn reaches the hub
- **THEN** the hub does not carry `retrieval`

#### Scenario: A function word, a number or a single letter is nobody's name
- **WHEN** anchors are titled with a leading stopword, a bare year or a single letter
  followed by a dash and a subtopic
- **THEN** no short name is derived for them and a turn using that word, year or letter
  does not give them `exact_alias`

#### Scenario: Singulars ending in e fold with their plurals
- **WHEN** a turn says "notes" and "files" and an anchor's terms include "note" and
  "file"
- **THEN** the anchor carries `lexical_overlap`

### Requirement: Bounded role lanes and the working-memory packet
For each resolved anchor the operation SHALL select context roles per the
`context-roles` capability and run one bounded lane per selected role over existing
primitives only: semantic units filtered by category within the anchor neighbourhood,
Records collection items for collections claiming the anchor, active Planning items
linked to the anchor, entity or profile page facets, `graph_context` under a
traversal profile from resolved anchors at depth at most 2, and evidence pointers. No
lane SHALL run for a `partial` anchor and nothing of a `partial` anchor's page or
neighbourhood SHALL enter `units`, `pointers` or `current_state`: a `partial` anchor is
listed in `anchors[]` with its status and evidence so the agent can choose it, and is
served only after it resolves or is chosen. The packet SHALL contain `anchors[]` (ref, title,
kind, status, evidence), `roles[]`, `units[]` (ref, role, text of at most 360
characters cut only at a boundary that leaves no unclosed wikilink, lifecycle,
updated, provenance), `pointers[]` (ref, title, why), `current_state[]` (anchor,
source, as_of, `statement` of at most 200 characters stating the observed status in
the source's own words), `missing[]` (role, reason), `ambiguity[]`,
`budget {limit_chars, used_chars}`, `generation {freshness_key, index_generation,
roles_hash}` and `abstained`. `used_chars` SHALL count every prose field of the
packet (unit text, `current_state[].statement`, pointer title and why) and SHALL
never exceed `max_chars`. Units SHALL be emitted before pages, pages beyond the
budget SHALL become pointers, and a superseded unit SHALL be marked `superseded`
with its active successor named rather than presented as current. A lane that
reaches its read limit before exhausting the anchor neighbourhood SHALL report
`missing[] {role, reason: "lane_truncated"}`, and a lane whose items fit neither as
units nor as pointers within `max_chars` SHALL report `missing[] {role, reason:
"budget"}`; an item the egress guard removes SHALL be reported as `missing[] {role,
reason: "withheld"}` once per affected section without naming the item, where `role`
holds the packet section name (`anchors`, `units`, `pointers`, `current_state`)
rather than a context role; the packet
never drops material silently. The packet SHALL NOT carry the
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

#### Scenario: A partial anchor beside a resolved one is listed, not served
- **WHEN** a turn resolves one anchor and reaches a second anchor by retrieved contact
  only
- **THEN** the second anchor appears in `anchors[]` as `partial`, and no unit, pointer
  or current-state entry in the packet comes from its page or its neighbourhood

#### Scenario: One authored word and one tag word do not resolve
- **WHEN** a turn shares one word with an anchor's title and one word with the anchor's
  tag or section heading, and nothing else reaches the anchor
- **THEN** the anchor carries `lexical_overlap` only, not `rare_term`, and is `partial`

#### Scenario: A turn made only of structural words reaches nothing
- **WHEN** a turn shares two words with an anchor's tags and section headings and none
  with its title or aliases, and the anchor's own page is a recall hit
- **THEN** the anchor does not carry `lexical_overlap` and is at most `partial`

