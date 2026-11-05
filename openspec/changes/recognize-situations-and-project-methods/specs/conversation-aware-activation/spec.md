## MODIFIED Requirements

### Requirement: Focus is resolved as part of the current turn

A `focus` string SHALL be resolved as a second segment of the current turn, contributing the worded contact kinds `exact_alias`, `lexical_overlap` and `claims_match`, and the qualifiers those kinds admit. This anchor-evidence resolution SHALL NOT run a recall query or an embedding, so it contributes neither `retrieval` nor `vector_band`. The independent maps candidate-discovery stage MAY use the bounded focus through existing retrieval, retaining `origin = "focus"`; its candidates SHALL NOT silently change anchor resolution.

Every served anchor SHALL carry an `origin` naming whose words reached it, so the agent can tell what the user said from what the agent itself said was in play:

- `turn`: reached by the turn segment only;
- `focus`: reached by the `focus` segment only;
- `turn_and_focus`: reached by both;
- `conversation`: carried from an earlier user entry (see the carry requirement below).

An anchor whose only contact came from `focus` SHALL keep `origin = "focus"` whatever qualifiers it gathers. A `focus` origin SHALL NOT count as the agent's `anchor` choice (`agent_choice`), and SHALL NOT override an ambiguity between anchors the turn segment itself resolved. The working-set hook's rendered block and the tool description SHALL state that `focus`-origin material is there because the agent named it, not because the user did. A packet built without `focus` SHALL label every anchor `turn`, or `conversation` when carried.

Tokens SHALL NOT pair across the two segments:

- An n-gram, a proximity window or a two-stem rarity pair SHALL be formed within one segment only.
- The retrieval carry, the referential test and the follow-up test SHALL read the turn segment alone. A turn that speaks only a referential cue therefore stays referential when a `focus` accompanies it.

#### Scenario: The agent's focus supplies the second domain

- **WHEN** a turn weighs "the budget trade-off we discussed", and the agent's `focus` names "Harbor Lantern budget versus the Kestrel hiring plan"
- **THEN** both anchors named by exact alias in `focus` resolve
- **AND** each carries `origin = "focus"`

#### Scenario: Focus cannot settle the user's own ambiguity

- **WHEN** the turn segment resolves two competing hubs, and `focus` names one of them
- **THEN** the packet stays `ambiguous`, and that hub carries `origin = "turn_and_focus"` in the ambiguity list

#### Scenario: Focus does not make a referential turn non-referential

- **WHEN** the turn is "continue" and a `focus` names one anchor by alias
- **THEN** the turn is still referential for the recency rules
- **AND** that anchor resolves through `focus` on its own evidence


### Requirement: Conversation evidence is a subordinate qualifier

Earlier turns (`recent`) and read refs (`refs`) SHALL contribute exactly one evidence kind, `conversation`. It is a qualifier, and it SHALL:

- never create a candidate;
- never resolve an anchor on its own, or together with qualifiers only;
- never count toward the two-kinds rule, except as provided below.

An anchor SHALL carry `conversation` when either of these holds:

- its canonical ref is among the visible refs;
- it is reached by `exact_alias` or `lexical_overlap` within one `recent` entry, under the turn's own token and stopword rules, evaluated per entry and never across entries.

Only the newest three `user` entries and the newest two `assistant` entries SHALL be read for this purpose. This qualifier path SHALL NOT embed conversation text or send it to recall or a model. The explicitly enabled context-fit interpreter MAY read the same bounded, scrubbed context under the context-interpretation contract. Its judgments SHALL remain request-scoped candidates and SHALL NOT count as anchor evidence or override ambiguity.

An anchor that carries `conversation` together with at least one contact kind reached by the current turn, in either segment, SHALL resolve. This is the same deliberate exception `continuity` has: the conversation already named that subject, and the current turn reached it again. `conversation` and `continuity` on one anchor SHALL count once.

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

