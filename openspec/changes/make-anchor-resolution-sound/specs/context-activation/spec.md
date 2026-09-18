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
exactly one kind other than `usage_prior` SHALL be `partial`. `retrieval` SHALL be
granted only when the anchor's own page is among the recall hits for the turn; a recall
hit elsewhere in the anchor's link neighbourhood SHALL NOT be contact.
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
regular plural and singular forms folded together. `lexical_overlap` SHALL be granted
for two or more shared terms. `rare_term` SHALL be granted for exactly one shared term
that occurs among the title and alias terms of no more anchors in the activation index
than the effective rare-term threshold (shipped default three); that count SHALL be
measured when the index is built and SHALL never leave the server. Turn tokens SHALL keep their order and
repetitions for n-gram construction so that two anchors sharing a word in their names
can both receive `exact_alias` from one turn. The index SHALL derive one short name
from an anchor title that carries a trailing parenthetical or dash qualifier and SHALL
treat it as an alias only while no other anchor's names include it and every term of it
is within the rare-term threshold, measured before derived names are added. `usage_prior` SHALL
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
