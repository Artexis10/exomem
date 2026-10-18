## MODIFIED Requirements

### Requirement: Conversation evidence is a subordinate qualifier

Earlier turns (`recent`) and read refs (`refs`) SHALL contribute exactly one evidence kind, `conversation`. It is a qualifier, and it SHALL:

- never create a candidate;
- never resolve an anchor on its own, or together with qualifiers only;
- never count toward the two-kinds rule, except as provided below.

An anchor SHALL carry `conversation` when either of these holds:

- its canonical ref is among the visible refs;
- it is reached by `exact_alias` or `lexical_overlap` within one `recent` entry, under the turn's own token and stopword rules, evaluated per entry and never across entries.

Only the newest three `user` entries and the newest two `assistant` entries SHALL be read for this purpose. Conversation text SHALL NOT be embedded, sent to recall, or read by any model.

An anchor that carries `conversation` together with at least one contact kind reached by the current turn, in either segment, SHALL resolve. An entity candidate marked as a singleton partial-name contact SHALL remain partial when prior naming supplies its only additional evidence. Independent current contact, full-name focus or agent choice MAY resolve it. This is the same deliberate exception `continuity` has: the conversation already named that subject, and the current turn reached it again. `conversation` and `continuity` on one anchor SHALL count once.

#### Scenario: The conversation promotes a partial second domain

- **WHEN** a turn reaches one anchor by `exact_alias` and a second only by `lexical_overlap`, and a `recent` user entry names the second by its alias
- **THEN** both anchors resolve, and the second's evidence is `[lexical_overlap, conversation]`
- **AND** the same turn without `conversation` resolves only the first and lists the second as `partial`

#### Scenario: The conversation alone resolves nothing

- **WHEN** a turn that is neither referential nor anaphoric reaches no anchor by any contact kind, and the earlier entries name three anchors by alias
- **THEN** the packet abstains `unresolved`, exactly as it does without `conversation`

#### Scenario: Words split across entries do not form a name

- **WHEN** one `recent` entry ends with "Harbor" and the next begins with "Lantern"
- **THEN** no anchor named "Harbor Lantern" carries `conversation` from that pair

#### Scenario: An unindexed name does not inherit a known name fragment

- **WHEN** a new unindexed name reaches an earlier entity through a contact marked as a singleton partial-name contact
- **THEN** prior conversation does not resolve that entity or serve its lane material
- **AND** the partial lead remains available for agent choice
