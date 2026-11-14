## MODIFIED Requirements

### Requirement: Categorical anchor evidence and resolution
Anchor candidates SHALL carry only categorical evidence kinds. Contact kinds —
`exact_alias`, `lexical_overlap`, `vector_band`, `claims_match` and `retrieval` —
establish that the turn reached the anchor; qualifier kinds — `category_match`,
`graph_corroboration` and `usage_prior` — strengthen an anchor the turn already reached
and SHALL never create a candidate on their own. No float score SHALL appear in the
packet. An anchor SHALL resolve as `resolved` when it carries `exact_alias` or at least
two independent kinds other than `usage_prior`, at least one of them a contact kind; as
`partial` when it carries exactly one kind other than `usage_prior`; after current-turn subject precedence, the turn SHALL be
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


## ADDED Requirements

### Requirement: Current-turn subject precedence
Recall SHALL retain a subject whose exact name appears in the current turn when unrelated unnamed weak alternatives would cause ambiguity. It SHALL retain those alternatives as partial leads. It SHALL check competing interpretations within each group of overlapping current-turn names independently. Focus-only names SHALL NOT activate this precedence.

#### Scenario: One explicit subject with an unrelated weak alternative
- **WHEN** the current turn names one resolved subject exactly and an unrelated weak match would compete with it
- **THEN** recall serves the named subject and retains the weak match as a partial lead

#### Scenario: Two separately named subjects remain available
- **WHEN** the current turn names two subjects with disjoint names
- **THEN** recall carries both subjects without treating their distinct names as competing senses

#### Scenario: A second topic does not hide same-name ambiguity
- **WHEN** one current-turn name resolves disconnected alternatives and the turn also names another topic
- **THEN** recall reports the first name's ambiguity even if cross-topic links connect the combined candidates

#### Scenario: Weak interpretations of a spoken name still compete
- **WHEN** a weaker candidate's current-turn name contact overlaps the exact subject name and represents another disconnected interpretation
- **THEN** recall preserves that ambiguity instead of treating the weaker interpretation as an unrelated task-word match

#### Scenario: Complementary weak context remains resolved
- **WHEN** a weak candidate is structurally complementary to the explicitly named subject
- **THEN** subject precedence does not demote that context solely because it lacks an exact name

#### Scenario: Focus does not impersonate the current turn
- **WHEN** only conversation focus supplies an exact name or adds an unspoken alias
- **THEN** recall does not treat that name or alias as current-turn spelling

#### Scenario: Weak-only and identity-selection behavior remains
- **WHEN** the current turn names no exact subject or an explicit identity choice selects the subject
- **THEN** recall preserves the existing weak-only ambiguity and identity-selection behavior
